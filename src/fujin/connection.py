from __future__ import annotations

import codecs
import hashlib
import shlex
import logging
import os
import secrets
import select
import socket
import tempfile
import time
import subprocess
from fujin.errors import UploadError
import sys
import termios
import tty
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

from ssh2.error_codes import LIBSSH2_ERROR_EAGAIN
from ssh2.exceptions import SCPProtocolError, SSH2Error
from ssh2.session import (
    LIBSSH2_FLAG_COMPRESS,
    LIBSSH2_SESSION_BLOCK_INBOUND,
    LIBSSH2_SESSION_BLOCK_OUTBOUND,
    Session,
)

from fujin.config import HostConfig
from fujin.errors import ConnectionError, SSHAuthenticationError, CommandError

logger = logging.getLogger(__name__)

#: Commands larger than this are spooled to a remote file and executed with
#: ``sh <file>``. Sending a large script as one exec-channel string can wedge
#: libssh2 forever (see deploy._write_remote_manifest for the same lesson).
SPOOL_THRESHOLD_BYTES = 4096


def should_spool(command: str) -> bool:
    """Whether *command* is too large to send through the exec channel."""
    return len(command.encode("utf-8")) > SPOOL_THRESHOLD_BYTES


class SSH2Connection:
    def __init__(self, session: Session, host: HostConfig, sock: socket.socket):
        self.session = session
        self.host = host
        self.cwd = ""
        self.sock = sock

    @contextmanager
    def cd(self, path: str) -> Generator[None, None, None]:
        """Context manager to temporarily change the working directory for commands.

        Args:
            path: Absolute or relative path to change to

        Yields:
            None
        """
        prev_cwd = self.cwd
        if path.startswith("/"):
            self.cwd = path
        elif self.cwd:
            self.cwd = f"{self.cwd}/{path}"
        else:
            self.cwd = path
        try:
            yield
        finally:
            self.cwd = prev_cwd

    def run(
        self,
        command: str,
        warn: bool = False,
        pty: bool = False,
        hide: bool = False,
        idle_timeout: float = 300,
    ) -> tuple[str, bool]:
        """Executes a command on the remote host.

        Non-PTY commands larger than :data:`SPOOL_THRESHOLD_BYTES` are
        uploaded to a private remote temp file and executed as
        ``sh <file>``: sending a big script through the exec channel in one
        string can wedge libssh2. The temp file is always removed afterwards.

        Args:
            command: The shell command to execute
            warn: If True, don't raise an exception on non-zero exit status
            pty: If True, allocate a pseudo-terminal for the command (enables password prompts, interactive shells)
            hide: If True, suppress stdout/stderr output. Can also be 'out' or 'err' to hide selectively
            idle_timeout: Seconds without any channel progress before the
                command is declared wedged and an error is raised. ``None``
                disables the deadline (interactive PTY sessions).

        Returns:
            A tuple of (stdout_output, success) where success is True if exit status was 0

        Raises:
            CommandError: The remote command exited non-zero (when
                ``warn=False``); ``code`` carries the remote exit status.
                ``app exec`` / ``server exec`` propagate that status
                verbatim, so they are outside the deploy exit-code
                taxonomy.
            ConnectionError: The SSH transport failed mid-command (socket
                or libssh2 error, a wedged channel, or EOF without an exit
                status). Exit code 5.
        """
        # Interactive sessions have no output deadline: the user may think.
        if pty:
            idle_timeout = None

        cwd_prefix = ""
        if self.cwd:
            logger.info(f"Changing directory to {self.cwd}")
            cwd_prefix = f"cd {self.cwd} && "

        # Add default paths to ensure uv is found
        env_prefix = (
            f"/home/{self.host.user}/.cargo/bin:/home/{self.host.user}/.local/bin:$PATH"
        )
        full_command = f'export PATH="{env_prefix}"; {cwd_prefix}{command}'
        logger.debug(f"Running command: {full_command}")

        # A large script sent as a single exec-channel string can wedge
        # libssh2, so spool it to a remote temp file and send a tiny
        # ``sh <file>`` directive instead. PTY sessions stay on the direct
        # path: they are interactive and tiny by nature.
        if not pty and should_spool(full_command):
            return self._run_spooled(full_command, command, warn, hide, idle_timeout)

        return self._execute(full_command, command, warn, pty, hide, idle_timeout)

    def run_script(
        self,
        script: str,
        *,
        sudo: bool = False,
        interpreter: str = "bash",
        warn: bool = False,
        hide: bool = False,
        idle_timeout: float = 300,
    ) -> tuple[str, bool]:
        """Run a (possibly large) shell script without a PTY.

        The script is spooled to a private remote temp file and executed as
        ``[sudo] <interpreter> <file>``. Use this for admin transactions that
        must not travel through the exec channel as one big string (they can
        wedge libssh2) and do not need a terminal. ``sudo`` here is the
        operator's full sudo (``server harden`` / ``create-user`` are admin
        actions, not the scoped deploy rule).
        """
        return self._run_spooled(
            script,
            script,
            warn,
            hide,
            idle_timeout,
            interpreter=interpreter,
            sudo=sudo,
        )

    def _run_spooled(
        self,
        full_command: str,
        command: str,
        warn: bool,
        hide: bool,
        idle_timeout: float | None,
        *,
        interpreter: str = "sh",
        sudo: bool = False,
    ) -> tuple[str, bool]:
        """Upload *full_command* to a private remote temp file and run it."""
        remote = f"/tmp/fujin-cmd-{os.getpid()}-{secrets.token_hex(8)}.sh"
        fd, local = tempfile.mkstemp(prefix="fujin-cmd-", suffix=".sh")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(full_command)
            # mkstemp already creates 0600; be explicit so the upload mode
            # handled to the remote side is never world-readable.
            os.chmod(local, 0o600)
            try:
                self.put(local, remote)
            except (SSH2Error, OSError) as exc:
                # Uploading the spool file is part of running the command:
                # a dying transport here is still exit code 5.
                raise ConnectionError(
                    "SSH transport failed while uploading the spooled command"
                    f" script: {command[:120]!r}: {exc}"
                ) from exc
            prefix = "sudo " if sudo else ""
            return self._execute(
                f"{prefix}{interpreter} {shlex.quote(remote)}",
                command,
                warn,
                False,
                hide,
                idle_timeout,
            )
        finally:
            self._remove_remote_script(remote)
            try:
                os.unlink(local)
            except OSError:
                logger.debug("Failed to remove local spool file", exc_info=True)

    def _remove_remote_script(self, remote: str) -> None:
        """Best-effort removal of a spooled remote script.

        Runs in a ``finally`` block, so it must never mask the original
        error: the rm is short and its own failures are swallowed.
        """
        try:
            self.run(
                f"rm -f {shlex.quote(remote)}",
                warn=True,
                hide=True,
                idle_timeout=30,
            )
        except BaseException:
            logger.debug("Failed to remove remote spool file", exc_info=True)

    def _execute(
        self,
        directive: str,
        command: str,
        warn: bool,
        pty: bool,
        hide: bool,
        idle_timeout: float | None,
    ) -> tuple[str, bool]:
        """Run *directive* on the remote host, streaming its output."""
        idle_deadline = None
        if idle_timeout is not None:
            idle_deadline = time.monotonic() + idle_timeout

        stdout_buffer = []
        stderr_buffer = []

        # Use incremental decoders to handle split UTF-8 characters across packets
        stdout_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        stderr_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

        channel = self.session.open_session()
        # this allow us to show output in near real-time
        self.session.set_blocking(False)

        # Save terminal settings if we are going to mess with them
        old_tty_attrs = None
        is_interactive = pty and sys.stdin.isatty()
        # Set when the try block fails: cleanup then must not wait for a
        # channel that may never reach EOF.
        failure: BaseException | None = None

        try:
            try:
                if pty:
                    channel.pty()
                channel.execute(directive)

                # Switch to raw mode for interactive sessions to prevent local echo
                # and handle password masking correctly.
                if is_interactive:
                    # this redcuces latency on keystrokes
                    # self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    old_tty_attrs = termios.tcgetattr(sys.stdin)
                    tty.setraw(sys.stdin.fileno())

                stdin_fd = -1
                try:
                    stdin_fd = sys.stdin.fileno()
                except (OSError, AttributeError):
                    pass

                sock_fd = self.sock.fileno()

                poller = select.poll()
                if stdin_fd >= 0:
                    poller.register(stdin_fd, select.POLLIN)
                poller.register(sock_fd, select.POLLIN)

                progress = [0]  # real payload bytes seen; drives the idle deadline

                def drain_channel() -> None:
                    while True:
                        size, data = channel.read()
                        if size == LIBSSH2_ERROR_EAGAIN:
                            break
                        if size < 0:
                            # Any other negative size is a libssh2 failure
                            # (e.g. socket disconnected), not a clean EOF.
                            raise ConnectionError(
                                "SSH transport failed while reading command"
                                f" output (libssh2 error {size}):"
                                f" {command[:120]!r}"
                            )
                        if size > 0:
                            progress[0] += size
                            text = stdout_decoder.decode(data)
                            if not hide or hide == "err":
                                sys.stdout.write(text)
                                sys.stdout.flush()
                            stdout_buffer.append(text)

                        else:
                            break

                    while True:
                        size, data = channel.read_stderr()
                        if size == LIBSSH2_ERROR_EAGAIN:
                            break
                        if size < 0:
                            raise ConnectionError(
                                "SSH transport failed while reading command"
                                " error output (libssh2 error"
                                f" {size}): {command[:120]!r}"
                            )
                        if size > 0:
                            progress[0] += size
                            text = stderr_decoder.decode(data)
                            if not hide or hide == "out":
                                sys.stderr.write(text)
                                sys.stderr.flush()
                            stderr_buffer.append(text)
                        else:
                            break

                while True:
                    directions = self.session.block_directions()

                    sock_events = select.POLLIN
                    if directions & LIBSSH2_SESSION_BLOCK_OUTBOUND:
                        sock_events |= select.POLLOUT
                    poller.modify(sock_fd, sock_events)

                    ready = poller.poll(250)

                    for fd, event in ready:
                        if fd == stdin_fd and event & select.POLLIN:
                            try:
                                data = os.read(stdin_fd, 8192)
                                if data:
                                    rc, _ = channel.write(data)
                                    while rc == LIBSSH2_ERROR_EAGAIN:
                                        poller.modify(sock_fd, select.POLLOUT)
                                        poller.poll(100)
                                        rc, _ = channel.write(data)
                                    if rc < 0:
                                        raise ConnectionError(
                                            "SSH transport failed while writing"
                                            " to the command channel (libssh2"
                                            f" error {rc}): {command[:120]!r}"
                                        )
                            except BlockingIOError:
                                pass

                        if fd == sock_fd and event & (
                            select.POLLIN | select.POLLERR | select.POLLHUP
                        ):
                            drain_channel()
                            if idle_timeout is not None:
                                idle_deadline = time.monotonic() + idle_timeout

                    if directions & LIBSSH2_SESSION_BLOCK_INBOUND:
                        seen = progress[0]
                        drain_channel()
                        if progress[0] != seen and idle_timeout is not None:
                            idle_deadline = time.monotonic() + idle_timeout

                    if channel.eof():
                        break

                    if (
                        idle_deadline is not None
                        and time.monotonic() > idle_deadline
                        and not channel.eof()
                    ):
                        # A channel that makes no progress is transport loss,
                        # not a remote failure: the remote exit status is
                        # unknown (5, not the deploy taxonomy's 4).
                        raise ConnectionError(
                            f"Remote command made no progress for {idle_timeout}s"
                            " and never reached EOF; the SSH channel was lost"
                            f" or is wedged: {command[:120]!r}"
                        )
            except (SSH2Error, OSError) as exc:
                # libssh2 and socket failures while reading/writing the
                # channel are transport loss: exit code 5.
                raise ConnectionError(
                    f"SSH transport failed while running {command[:120]!r}: {exc}"
                ) from exc
        except BaseException as exc:
            failure = exc
            raise
        finally:
            # Cleanup is best-effort: it must never mask the original
            # exception (which carries the exit-code taxonomy) and must not
            # block forever on a channel whose transport is gone.
            if old_tty_attrs:
                try:
                    termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_tty_attrs)
                except Exception:
                    logger.debug("Failed to restore terminal attributes", exc_info=True)
            try:
                self.session.set_blocking(True)
            except Exception:
                logger.debug("set_blocking(True) failed", exc_info=True)
            if failure is None:
                try:
                    channel.wait_eof()
                except Exception:
                    logger.debug("wait_eof failed during cleanup", exc_info=True)
            try:
                channel.close()
            except Exception:
                logger.debug("channel.close failed during cleanup", exc_info=True)
            try:
                channel.wait_closed()
            except Exception:
                logger.debug("channel.wait_closed failed during cleanup", exc_info=True)

        try:
            exit_status = channel.get_exit_status()
        except (SSH2Error, OSError) as exc:
            raise ConnectionError(
                "SSH transport failed before the remote exit status was"
                f" received: {command[:120]!r}: {exc}"
            ) from exc
        if exit_status < 0:
            # libssh2 reports a negative status when the channel died before
            # the remote side sent an exit status (mid-command transport
            # loss). A genuine remote failure is a non-negative status.
            raise ConnectionError(
                "SSH transport failed mid-command (no exit status received,"
                f" libssh2 status {exit_status}): {command[:120]!r}"
            )
        if exit_status != 0 and not warn:
            raise CommandError(
                f"Command failed with exit code {exit_status}", code=exit_status
            )

        return "".join(stdout_buffer), exit_status == 0

    def put(self, local: str, remote: str, verify: bool = False) -> None:
        """Uploads a local file to the remote host using SCP.

        Args:
            local: Path to the local file to upload
            remote: Destination path on the remote host (absolute or relative to cwd)
            verify: If True, verify the upload with SHA256 checksum comparison

        Raises:
            FileNotFoundError: If the local file doesn't exist
            ValueError: If the local path is not a file
            UploadError: If checksum verification fails (when verify=True)
        """
        local_path = Path(local)

        if not local_path.exists():
            raise FileNotFoundError(f"Local file not found: {local}")

        if not local_path.is_file():
            raise ValueError(f"Local path is not a file: {local}")

        fileinfo = local_path.stat()

        # If remote path is relative, prepend cwd
        if not remote.startswith("/") and self.cwd:
            remote = f"{self.cwd}/{remote}"

        local_checksum = None
        if verify:
            with open(local, "rb") as f:
                local_checksum = hashlib.file_digest(f, "sha256").hexdigest()

        try:
            channel = self.session.scp_send64(
                remote,
                fileinfo.st_mode & 0o777,
                fileinfo.st_size,
                fileinfo.st_mtime,
                fileinfo.st_atime,
            )
        except SCPProtocolError as e:
            raise UploadError(
                f"SCP upload failed for {remote}\n"
                "Possible causes:\n"
                "  - Server not bootstrapped (run: fujin server bootstrap)\n"
                "  - Permission denied (SSH user may not have write access)\n"
                "  - Remote directory does not exist\n"
                "  - SSH session has stale group membership (reconnect to server)"
            ) from e

        try:
            with open(local, "rb") as local_fh:
                # Read in 512KB chunks for better throughput
                while True:
                    data = local_fh.read(524288)
                    if not data:
                        break
                    channel.write(data)
        finally:
            channel.close()

        if verify and local_checksum:
            remote_q = shlex.quote(remote)
            remote_checksum_out, _ = self.run(
                f"sha256sum {remote_q} | awk '{{print $1}}'",
                hide=True,
            )
            remote_checksum = remote_checksum_out.strip()

            if local_checksum != remote_checksum:
                raise UploadError(
                    f"Upload verification failed! Local: {local_checksum}, Remote: {remote_checksum}",
                    checksum_mismatch=True,
                )

    def get(self, remote: str, local: str, verify: bool = False) -> None:
        """Downloads a remote file to the local path via system ``ssh cat``.

        Large file reads must NOT go through the embedded session — neither
        ``run("cat …")`` (exec-channel reads can trickle forever) nor scp
        (needs an sftp subsystem or a remote scp binary, neither of which
        exists on every target). System ssh is the transport the rsync path
        already uses; its exec channel is proven solid.

        Password-auth hosts are not supported here (BatchMode); callers read
        small files via ``run()`` instead.
        """
        remote_path = remote
        if not remote.startswith("/") and self.cwd:
            remote_path = f"{self.cwd}/{remote}"

        cmd = ["ssh", "-o", "BatchMode=yes", "-p", str(self.host.port)]
        if self.host.key_filename:
            cmd.extend(["-i", str(Path(self.host.key_filename).expanduser().resolve())])
        cmd.extend([f"{self.host.user}@{self.host.address}", "cat", "--", remote_path])
        with open(local, "wb") as out_fh:
            result = subprocess.run(
                cmd, stdout=out_fh, stderr=subprocess.PIPE, timeout=120
            )
        if result.returncode != 0:
            import os as _os

            _os.unlink(local)
            raise UploadError(
                f"Download failed for {remote_path} (exit {result.returncode}):\n"
                f"{result.stderr.decode(errors='replace').strip()}"
            )

        if verify:
            remote_q = shlex.quote(remote_path)
            out, ok = self.run(f"sha256sum {remote_q} | awk '{{print $1}}'", hide=True)
            if not ok:
                raise UploadError(f"Could not checksum {remote_path} on server")
            with open(local, "rb") as f:
                local_checksum = hashlib.file_digest(f, "sha256").hexdigest()
            if local_checksum != out.strip():
                raise UploadError(
                    f"Download verification failed! Local: {local_checksum},"
                    f" Remote: {out.strip()}"
                )

    def rsync_upload(self, local: str, remote: str) -> None:
        local_path = Path(local)

        # Build rsync command with SSH options
        ssh_opts = ["-p", str(self.host.port)]
        if self.host.key_filename:
            key_path = Path(self.host.key_filename).expanduser().resolve()
            ssh_opts.extend(["-i", str(key_path)])

        rsync_cmd = [
            "rsync",
            "-az",  # archive mode, compress
            "--progress",
            "-e",
            "ssh " + " ".join(ssh_opts),
            str(local_path),
            f"{self.host.user}@{self.host.address}:{remote}",
        ]

        logger.info(f"Using rsync for delta upload to {remote}")
        result = subprocess.run(rsync_cmd)

        if result.returncode != 0:
            raise UploadError(f"rsync failed with exit code {result.returncode}")


@contextmanager
def connection(
    host: HostConfig, *, compress: bool = True
) -> Generator[SSH2Connection, None, None]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        logger.info(f"Connecting to {host.address}:{host.port}...")
        sock.settimeout(30)
        sock.connect((host.address, host.port))
        sock.settimeout(None)
        # disable Nagle's algorithm for lower latency
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        # keepalive to prevent NAT/firewall from dropping long-running connections
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 60)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
    except socket.error as e:
        raise ConnectionError(f"Failed to connect to {host.address}:{host.port}") from e

    session = Session()
    if compress:
        session.flag(LIBSSH2_FLAG_COMPRESS, True)
    try:
        logger.info("Starting SSH session...")
        session.handshake(sock)
    except Exception as e:
        sock.close()
        raise ConnectionError("SSH Handshake failed") from e

    logger.info("Authenticating...")
    auth_methods_tried = []
    authenticated = False

    # For hosts that require native SSH auth (Tailscale, etc.), authenticate via ssh first
    if host.native_ssh_auth:
        logger.info("Using native SSH for authentication...")
        ssh_cmd = [
            "ssh",
            "-p",
            str(host.port),
            "-o",
            "StrictHostKeyChecking=accept-new",
            f"{host.user}@{host.address}",
            "exit",
        ]
        result = subprocess.run(ssh_cmd)
        if result.returncode == 0:
            # Reconnect after successful auth - session is now authorized
            sock.close()
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(30)
            sock.connect((host.address, host.port))
            sock.settimeout(None)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 60)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
            session = Session()
            if compress:
                session.flag(LIBSSH2_FLAG_COMPRESS, True)
            session.handshake(sock)
            auth_methods_tried.append("native ssh")
            # Continue to normal auth methods below (they should work now)

    # Method 1: Explicit key file (if specified)
    if not authenticated and host.key_filename:
        try:
            key_path = Path(host.key_filename).expanduser()
            logger.debug(f"Trying explicit key: {key_path}")
            passphrase = host.key_passphrase or ""
            session.userauth_publickey_fromfile(host.user, str(key_path), passphrase)
            authenticated = session.userauth_authenticated()
            if authenticated:
                logger.info(f"✓ Authenticated using {key_path}")
            auth_methods_tried.append(f"key file: {key_path}")
        except Exception as e:
            logger.debug(f"Key file auth failed: {e}")
            auth_methods_tried.append(f"key file: {key_path} (failed)")

    # Method 2: SSH agent (loaded keys)
    if not authenticated:
        try:
            logger.debug("Trying ssh-agent...")
            session.agent_auth(host.user)
            authenticated = session.userauth_authenticated()
            if authenticated:
                logger.info("✓ Authenticated using ssh-agent")
            auth_methods_tried.append("ssh-agent")
        except Exception as e:
            logger.debug(f"Agent auth failed: {e}")
            auth_methods_tried.append("ssh-agent (failed)")

    # Method 3: Common key locations (fallback)
    if not authenticated:
        common_keys = [
            "~/.ssh/id_ed25519",
            "~/.ssh/id_rsa",
            "~/.ssh/id_ecdsa",
            "~/.ssh/id_dsa",
        ]

        for key_path_str in common_keys:
            if authenticated:
                break

            key_path = Path(key_path_str).expanduser()
            if not key_path.exists():
                continue

            try:
                logger.debug(f"Trying default key: {key_path}")
                # Try with empty passphrase (most common case)
                session.userauth_publickey_fromfile(host.user, str(key_path), "")
                authenticated = session.userauth_authenticated()

                if authenticated:
                    logger.info(f"✓ Authenticated using {key_path}")
                    auth_methods_tried.append(f"default key: {key_path}")
                    break

            except Exception as e:
                logger.debug(f"Key {key_path} failed: {e}")

    # Method 4: Password auth is no longer supported; key/agent auth or
    # native_ssh_auth only.

    if not authenticated:
        sock.close()
        methods_str = ", ".join(auth_methods_tried) if auth_methods_tried else "none"
        host_str = f"{host.user}@{host.address}"

        error_msg = (
            f"Authentication failed for {host_str}\n"
            f"Tried: {methods_str}\n\n"
            f"Solutions:\n"
            f"  1. Ensure your SSH key is authorized on the server\n"
            f"  2. Add your key to ssh-agent: ssh-add ~/.ssh/id_ed25519\n"
            f'  3. Specify key in fujin.toml: key_filename = "~/.ssh/id_ed25519"\n'
            f"  4. Use `fujin server setup-ssh` to copy your key to the server\n"
        )
        raise SSHAuthenticationError(error_msg)

    conn = SSH2Connection(session, host, sock=sock)
    try:
        yield conn
    finally:
        try:
            session.disconnect()
        except Exception as e:
            logger.warning(f"Error disconnecting session: {e}")
        finally:
            sock.close()
