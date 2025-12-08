from __future__ import annotations

from pathlib import Path
import socket
import sys
import re
import os
import logging
import cappa
from contextlib import contextmanager
from typing import Generator
from fujin.config import HostConfig
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

                            if "sudo" in text and watchers:
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
            raise cappa.Exit(
                f"Command failed with exit code {exit_status}", code=exit_status
            )

        return "".join(stdout_buffer), exit_status == 0

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
        logger.info(f"Connecting to {host.ip}:{host.ssh_port}...")
        sock.settimeout(30)
        sock.connect((host.ip or host.domain_name, host.ssh_port))
        sock.settimeout(None)
        # disable Nagle's algorithm for lower latency
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except socket.error as e:
        raise cappa.Exit(f"Failed to connect to {host.ip}:{host.ssh_port}") from e

    session = Session()
    try:
        logger.info("Starting SSH session...")
        session.handshake(sock)
    except Exception as e:
        sock.close()
        raise cappa.Exit("SSH Handshake failed") from e

    logger.info("Authenticating...")
    try:
        if host.key_filename:
            logger.debug(
                "Authenticating with public key from file %s", host.key_filename
            )
            passphrase = host.key_passphrase or ""
            session.userauth_publickey_fromfile(
                host.user, str(host.key_filename), passphrase
            )
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
