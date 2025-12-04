from __future__ import annotations

from pathlib import Path
import socket
import sys
import re
import logging
import cappa
from contextlib import contextmanager
from typing import Generator
from fujin.config import HostConfig
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
    def cd(self, path: str):
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
        """
        Executes a command on the remote host.
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

        watchers, pass_response = None, None
        if self.host.password:
            logger.debug("Setting up sudo password watchers")
            watchers = (
                re.compile(r"\[sudo\] password:"),
                re.compile(rf"\[sudo\] password for {self.host.user}:"),
            )
            pass_response = self.host.password + "\n"

        stdout_buffer = []
        stderr_buffer = []

        channel = self.session.open_session()
        # this allow us to show output in near real-time
        self.session.set_blocking(False)

        try:
            if pty:
                channel.pty()
            channel.execute(full_command)

            while True:
                # Read stdout
                size, data = channel.read()
                if size == LIBSSH2_ERROR_EAGAIN:
                    self._wait_for_socket()  # wait for readiness
                elif size > 0:
                    text = data.decode("utf-8", errors="replace")
                    if not hide or hide == "err":
                        sys.stdout.write(text)
                        sys.stdout.flush()
                    stdout_buffer.append(text)

                    if "sudo" in text and watchers:
                        for pattern in watchers:
                            if pattern.search(text):
                                logger.debug(
                                    "Password pattern matched, sending response"
                                )
                                channel.write(pass_response)

                # Read stderr
                size_err, data_err = channel.read_stderr()
                if size_err == LIBSSH2_ERROR_EAGAIN:
                    self._wait_for_socket()
                elif size_err > 0:
                    text_err = data_err.decode("utf-8", errors="replace")
                    if not hide or hide == "out":
                        sys.stderr.write(text_err)
                        sys.stderr.flush()
                    stderr_buffer.append(text_err)

                try:
                    eof = channel.eof()
                except Exception:
                    eof = False

                if eof:
                    break

        finally:
            self.session.set_blocking(True)
            channel.wait_eof()
            channel.close()
            channel.wait_closed()

        exit_status = channel.get_exit_status()
        if exit_status != 0 and not warn:
            raise cappa.Exit(
                f"Command failed with exit code {exit_status}", code=exit_status
            )

        return "".join(stdout_buffer), exit_status == 0

    def _wait_for_socket(self, timeout=1.0):
        """Wait until the session socket is ready for read/write."""
        directions = self.session.block_directions()

        if directions == 0:
            return

        rlist = [self.sock] if directions & LIBSSH2_SESSION_BLOCK_INBOUND else []
        wlist = [self.sock] if directions & LIBSSH2_SESSION_BLOCK_OUTBOUND else []

        select(rlist, wlist, [], timeout)

    def put(self, local: str, remote: str):
        """
        Uploads a local file to the remote host.
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
                for data in local_fh:
                    channel.write(data)
        finally:
            channel.close()


@contextmanager
def connection(host: HostConfig) -> Generator[SSH2Connection, None, None]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        logger.info(f"Connecting to {host.ip}:{host.ssh_port}...")
        sock.connect((host.ip or host.domain_name, host.ssh_port))
    except socket.error as e:
        raise Exception(f"Failed to connect to {host.ip}:{host.ssh_port}") from e

    session = Session()
    try:
        logger.info("Starting SSH session...")
        session.handshake(sock)
    except Exception as e:
        sock.close()
        raise Exception("SSH Handshake failed") from e

    logger.info("Authenticating...")
    try:
        # TODO: maybe add support for passphrase later
        if host.key_filename:
            logger.debug(
                "Authenticating with public key from file %s", host.key_filename
            )
            session.userauth_publickey_fromfile(host.user, str(host.key_filename), "")
        elif host.password:
            logger.debug("Authenticating with password")
            session.userauth_password(host.user, host.password)
        else:
            logger.debug("Authenticating with SSH agent...")
            session.agent_auth(host.user)

    except Exception as e:
        sock.close()
        raise cappa.Exit(f"Authentication failed for {host.user}") from e

    if not session.userauth_authenticated():
        raise cappa.Exit("Authentication failed")

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
