from __future__ import annotations

import socket
import os
import sys
import re
import logging
import cappa
from contextlib import contextmanager
from typing import Generator
from fujin.config import HostConfig
from ssh2.session import Session

logger = logging.getLogger(__name__)


from dataclasses import dataclass


@dataclass
class CommandResult:
    stdout: str
    stderr: str
    return_code: int
    ok: bool


class SSH2Connection:
    def __init__(self, session: Session, host: HostConfig):
        self.session = session
        self.host = host
        self.cwd = ""

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
        env: dict[str, str] | None = None,
        warn: bool = False,
        pty: bool = False,
        hide: bool = False,
    ) -> int:
        """
        Executes a command on the remote host.
        Mimics fabric.Connection.run behavior partially.
        """
        channel = self.session.open_session()

        if pty:
            channel.pty()

        # Ensure env is a dict
        env = env or {}

        # Add default paths to ensure uv is found
        extra_paths = [
            f"/home/{self.host.user}/.cargo/bin",
            f"/home/{self.host.user}/.local/bin",
        ]
        path_str = ":".join(extra_paths)

        if "PATH" in env:
            env["PATH"] = f"{path_str}:{env['PATH']}"
        else:
            env["PATH"] = f"{path_str}:$PATH"

        # Handle env vars by prepending them to the command
        # This avoids AcceptEnv issues on the server
        # We use double quotes to allow variable expansion (e.g. $PATH)
        env_pairs = []
        for k, v in env.items():
            escaped_v = v.replace('"', '\\"')
            env_pairs.append(f'{k}="{escaped_v}"')

        env_prefix = " ".join(env_pairs) + " "

        # Handle cwd
        cwd_prefix = ""
        if self.cwd:
            cwd_prefix = f"cd {self.cwd} && "

        full_command = f"{cwd_prefix}{env_prefix}{command}"
        logger.debug(f"Running command: {full_command}")

        watchers = []
        if self.host.password:
            watchers.append(
                (re.compile(r"\[sudo\] password:"), f"{self.host.password}\n")
            )
            watchers.append(
                (
                    re.compile(rf"\[sudo\] password for {self.host.user}:"),
                    f"{self.host.password}\n",
                )
            )

        channel.execute(full_command)

        # Set non-blocking to allow polling both streams
        self.session.set_blocking(False)

        stdout_buffer = []
        stderr_buffer = []

        import time

        while not channel.eof():
            # Read stdout
            size, data = channel.read()
            if size > 0:
                text = data.decode("utf-8", errors="replace")
                if not hide:
                    sys.stdout.write(text)
                    sys.stdout.flush()
                stdout_buffer.append(text)

                for pattern, response in watchers:
                    if pattern.search(text):
                        channel.write(response)

            # Read stderr
            size, data = channel.read_stderr()
            if size > 0:
                text = data.decode("utf-8", errors="replace")
                if not hide:
                    sys.stderr.write(text)
                    sys.stderr.flush()
                stderr_buffer.append(text)

            # Sleep briefly to avoid 100% CPU usage
            time.sleep(0.01)

        self.session.set_blocking(True)

        channel.wait_eof()
        channel.close()
        channel.wait_closed()

        exit_status = channel.get_exit_status()
        if exit_status != 0 and not warn:
            raise cappa.Exit(
                f"Command failed with exit code {exit_status}", code=exit_status
            )

        return CommandResult(
            stdout="".join(stdout_buffer),
            stderr="".join(stderr_buffer),
            return_code=exit_status,
            ok=exit_status == 0,
        )

    def put(self, local: str, remote: str):
        """
        Uploads a local file to the remote host.
        Mimics fabric.Connection.put behavior partially.
        """
        fileinfo = os.stat(local)

        # If remote path is relative, prepend cwd
        if not remote.startswith("/") and self.cwd:
            remote = f"{self.cwd}/{remote}"

        chan = self.session.scp_send64(
            remote,
            fileinfo.st_mode & 0o777,
            fileinfo.st_size,
            fileinfo.st_mtime,
            fileinfo.st_atime,
        )

        with open(local, "rb") as local_fh:
            for data in local_fh:
                chan.write(data)

    def shell(self):
        """
        Starts an interactive shell session.
        """
        import select
        import termios
        import tty

        channel = self.session.open_session()
        channel.pty()
        channel.shell()

        # Save original tty settings
        old_tty = termios.tcgetattr(sys.stdin)
        try:
            # Set raw mode for proper interaction
            tty.setraw(sys.stdin.fileno())
            self.session.set_blocking(False)

            while not channel.eof():
                # Read from channel
                size, data = channel.read()
                if size > 0:
                    sys.stdout.write(data.decode("utf-8", errors="replace"))
                    sys.stdout.flush()

                # Read from stdin
                r, _, _ = select.select([sys.stdin], [], [], 0.01)
                if sys.stdin in r:
                    x = sys.stdin.read(1)
                    if len(x) == 0:
                        break
                    channel.write(x)
        finally:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_tty)
            channel.close()
            channel.wait_closed()


@contextmanager
def host_connection(host: HostConfig) -> Generator[SSH2Connection, None, None]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.connect((host.ip or host.domain_name, host.ssh_port))
    except socket.error as e:
        raise Exception(f"Failed to connect to {host.ip}:{host.ssh_port}") from e

    session = Session()
    try:
        session.handshake(sock)
    except Exception as e:
        sock.close()
        raise Exception("SSH Handshake failed") from e

    # Authentication
    try:
        # maybe add support for passphrase later
        if host.key_filename:
            session.userauth_publickey_fromfile(host.user, str(host.key_filename), "")
        elif host.password:
            session.userauth_password(host.user, host.password)
        else:
            session.agent_auth(host.user)

    except Exception as e:
        sock.close()
        raise Exception(f"Authentication failed for {host.user}") from e

    conn = SSH2Connection(session, host)
    try:
        yield conn
    finally:
        # session.disconnect() # ssh2-python session doesn't have disconnect, closing socket is enough
        sock.close()
