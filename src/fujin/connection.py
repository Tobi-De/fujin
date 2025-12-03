from __future__ import annotations

import socket
import os
import sys
import re
import logging
import cappa
import time
from contextlib import contextmanager
from typing import Generator
from fujin.config import HostConfig
from ssh2.session import Session

logger = logging.getLogger(__name__)


class SSH2Connection:
    def __init__(self, session: Session, host: HostConfig):
        self.session = session
        self.host = host
        self.cwd = ""
        self._prefix = None

    @contextmanager
    def prefix(self, command: str):
        self._prefix = command
        try:
            yield
        finally:
            self._prefix = None

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
    ) -> tuple[str, bool]:
        """
        Executes a command on the remote host.
        """
        channel = self.session.open_session()
        if pty:
            channel.pty()
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
            logger.info(f"Changing directory to {self.cwd}")
            cwd_prefix = f"cd {self.cwd} && "

        # Handle prefix
        prefix_str = ""
        if self._prefix:
            prefix_str = f"{self._prefix} && "

        full_command = f"{cwd_prefix}{prefix_str}{env_prefix}{command}"
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
        stdout_buffer = []
        stderr_buffer = []

        try:
            # this makes channel reads non-blocking
            self.session.set_blocking(False)
            while not channel.eof():
                # Read stdout
                size, data = channel.read()
                if size > 0:
                    text = data.decode("utf-8", errors="replace")
                    if not hide or (isinstance(hide, str) and hide == "err"):
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
                    if not hide or (isinstance(hide, str) and hide == "out"):
                        sys.stderr.write(text)
                        sys.stderr.flush()
                    stderr_buffer.append(text)

                # Sleep briefly to avoid 100% CPU usage
                time.sleep(0.01)
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

    def put(self, local: str, remote: str):
        """
        Uploads a local file to the remote host.
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

    # Authentication
    logger.info("Authenticating...")
    try:
        # maybe add support for passphrase later
        if host.key_filename:
            logger.debug(
                "Authenticating with public key from file %s", host.key_filename
            )
            session.userauth_publickey_fromfile(host.user, str(host.key_filename), "")
        elif host.password:
            logger.debug("Authenticating with password %s", host.password)
            session.userauth_password(host.user, host.password)
        else:
            logger.debug("Authenticating with SSH agent...")
            session.agent_auth(host.user)

    except Exception as e:
        sock.close()
        raise cappa.Exit(f"Authentication failed for {host.user}") from e

    conn = SSH2Connection(session, host)
    try:
        yield conn
    finally:
        session.disconnect()
        sock.close()
