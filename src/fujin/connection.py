from __future__ import annotations

from pathlib import Path
import socket
import sys
import re
import os
import logging
from contextlib import contextmanager
from typing import Generator
from fujin.config import HostConfig
from fujin.errors import ConnectionError, SSHAuthenticationError
import termios
import tty
import codecs

from ssh2.session import (
    Session,
    LIBSSH2_SESSION_BLOCK_INBOUND,
    LIBSSH2_SESSION_BLOCK_OUTBOUND,
)
from ssh2.error_codes import LIBSSH2_ERROR_EAGAIN
from select import select

logger = logging.getLogger(__name__)


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
    ) -> tuple[str, bool]:
        """Executes a command on the remote host.

        Args:
            command: The shell command to execute
            warn: If True, don't raise an exception on non-zero exit status
            pty: If True, allocate a pseudo-terminal for the command (enables password prompts, interactive shells)
            hide: If True, suppress stdout/stderr output. Can also be 'out' or 'err' to hide selectively

        Returns:
            A tuple of (stdout_output, success) where success is True if exit status was 0

        Raises:
            cappa.Exit: If the command fails and warn=False
        """

        cwd_prefix = ""
        if self.cwd:
            logger.info(f"Changing directory to {self.cwd}")
            cwd_prefix = f"cd {self.cwd} && "

        # Add default paths to ensure uv is found
        env_prefix = (
            f"/home/{self.host.user}/.cargo/bin:/home/{self.host.user}/.local/bin:$PATH"
        )
        full_command = f'export PATH="{env_prefix}" && {cwd_prefix}{command}'
        logger.debug(f"Running command: {full_command}")

        watchers: tuple[re.Pattern[str], ...] | None = None
        pass_response: str | None = None
        if self.host.password:
            logger.debug("Setting up sudo password watchers")
            watchers = (
                re.compile(r"\[sudo\] password:"),
                re.compile(rf"\[sudo\] password for {self.host.user}:"),
            )
            pass_response = self.host.password + "\n"

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

        try:
            if pty:
                channel.pty()
            channel.execute(full_command)

            # Switch to raw mode for interactive sessions to prevent local echo
            # and handle password masking correctly.
            if is_interactive:
                # this redcuces latency on keystrokes
                # self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                old_tty_attrs = termios.tcgetattr(sys.stdin)
                tty.setraw(sys.stdin.fileno())

            while True:
                # Determine what libssh2 needs
                directions = self.session.block_directions()

                read_fds = [sys.stdin]
                write_fds = []

                # If libssh2 wants to READ from network
                if directions & LIBSSH2_SESSION_BLOCK_INBOUND:
                    read_fds.append(self.sock)

                # If libssh2 wants to WRITE to network
                if directions & LIBSSH2_SESSION_BLOCK_OUTBOUND:
                    write_fds.append(self.sock)

                # Wait until something is ready
                r_ready, *_ = select(read_fds, write_fds, [], 1.0)

                if sys.stdin in r_ready:
                    try:
                        data = os.read(sys.stdin.fileno(), 1024)
                        if data:
                            # User typed something → send to SSH channel
                            rc, _ = channel.write(data)
                            while rc == LIBSSH2_ERROR_EAGAIN:
                                select([], [self.sock], [], 1.0)
                                rc, _ = channel.write(data)
                    except BlockingIOError:
                        pass

                if self.sock in r_ready or (directions & LIBSSH2_SESSION_BLOCK_INBOUND):
                    # Read stdout
                    while True:
                        size, data = channel.read()
                        if size == LIBSSH2_ERROR_EAGAIN:
                            break
                        if size > 0:
                            text = stdout_decoder.decode(data)
                            if not hide or hide == "err":
                                sys.stdout.write(text)
                                sys.stdout.flush()
                            stdout_buffer.append(text)

                            if "sudo" in text and watchers and pass_response:
                                for pattern in watchers:
                                    if pattern.search(text):
                                        logger.debug(
                                            "Password pattern matched, sending response"
                                        )
                                        channel.write(pass_response.encode())
                        else:
                            break

                    # Read stderr
                    while True:
                        size, data = channel.read_stderr()
                        if size == LIBSSH2_ERROR_EAGAIN:
                            break
                        if size > 0:
                            text = stderr_decoder.decode(data)
                            if not hide or hide == "out":
                                sys.stderr.write(text)
                                sys.stderr.flush()
                            stderr_buffer.append(text)
                        else:
                            break

                if channel.eof():
                    break

        finally:
            if old_tty_attrs:
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_tty_attrs)
            self.session.set_blocking(True)
            channel.wait_eof()
            channel.close()
            channel.wait_closed()

        exit_status = channel.get_exit_status()
        if exit_status != 0 and not warn:
            raise ConnectionError(
                f"Command failed with exit code {exit_status}", code=exit_status
            )

        return "".join(stdout_buffer), exit_status == 0

    def put(self, local: str, remote: str) -> None:
        """Uploads a local file to the remote host using SCP.

        Args:
            local: Path to the local file to upload
            remote: Destination path on the remote host (absolute or relative to cwd)

        Raises:
            FileNotFoundError: If the local file doesn't exist
            ValueError: If the local path is not a file
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

        channel = self.session.scp_send64(
            remote,
            fileinfo.st_mode & 0o777,
            fileinfo.st_size,
            fileinfo.st_mtime,
            fileinfo.st_atime,
        )

        try:
            with open(local, "rb") as local_fh:
                # Read in 128KB chunks
                while True:
                    data = local_fh.read(131072)
                    if not data:
                        break
                    channel.write(data)
        finally:
            channel.close()


@contextmanager
def connection(host: HostConfig) -> Generator[SSH2Connection, None, None]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        logger.info(f"Connecting to {host.address}:{host.port}...")
        sock.settimeout(30)
        sock.connect((host.address, host.port))
        sock.settimeout(None)
        # disable Nagle's algorithm for lower latency
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except socket.error as e:
        raise ConnectionError(f"Failed to connect to {host.address}:{host.port}") from e

    session = Session()
    try:
        logger.info("Starting SSH session...")
        session.handshake(sock)
    except Exception as e:
        sock.close()
        raise ConnectionError("SSH Handshake failed") from e

    logger.info("Authenticating...")
    auth_methods_tried = []
    authenticated = False
    auth_method_used = None

    # Method 1: Explicit key file (if specified)
    if host.key_filename:
        try:
            key_path = Path(host.key_filename).expanduser()
            logger.debug(f"Trying explicit key: {key_path}")
            passphrase = host.key_passphrase or ""
            session.userauth_publickey_fromfile(host.user, str(key_path), passphrase)
            authenticated = session.userauth_authenticated()
            if authenticated:
                auth_method_used = f"key file: {key_path}"
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

    # Method 4: Password (if configured)
    if not authenticated and host.password:
        try:
            logger.debug("Trying password authentication...")
            session.userauth_password(host.user, host.password)
            authenticated = session.userauth_authenticated()
            if authenticated:
                logger.info("✓ Authenticated using password")
            auth_methods_tried.append("password")
        except Exception as e:
            logger.debug(f"Password auth failed: {e}")
            auth_methods_tried.append("password (failed)")

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
            f"  4. Set password in fujin.toml or .env file"
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
