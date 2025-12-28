from contextlib import contextmanager
from dataclasses import dataclass
from functools import cached_property
from typing import Generator

import cappa

from fujin.config import Config
from fujin.connection import SSH2Connection
from fujin.connection import connection as host_connection


@dataclass
class BaseCommand:
    """
    A command that provides access to the host config and provide a connection to interact with it,
    including configuring the web proxy and managing systemd services.
    """

    @cached_property
    def config(self) -> Config:
        return Config.read()

    @cached_property
    def stdout(self) -> cappa.Output:
        return cappa.Output()

    @contextmanager
    def connection(self) -> Generator[SSH2Connection, None, None]:
        with host_connection(host=self.config.host) as conn:
            yield conn


def install_archive_script(remote_path: str, app_name: str, version: str) -> str:
    remote_extract_dir = f"/tmp/{app_name}-{version}"
    install_cmd = (
        f"mkdir -p {remote_extract_dir} && "
        f"tar --overwrite -xzf {remote_path} -C {remote_extract_dir} && "
        f"cd {remote_extract_dir} && "
        f"chmod +x install.sh && "
        f"bash ./install.sh || (echo 'install failed' >&2; exit 1) && "
        f"cd / && rm -rf {remote_extract_dir}"
    )
    return install_cmd


def uninstall_archive_script(remote_path: str, app_name: str, version: str) -> str:
    remote_extract_dir = f"/tmp/uninstall-{app_name}-{version}"
    uninstall_cmd = (
        f"mkdir -p {remote_extract_dir} && "
        f"tar --overwrite -xzf {remote_path} -C {remote_extract_dir} && "
        f"cd {remote_extract_dir} && "
        f"chmod +x uninstall.sh && "
        f"bash ./uninstall.sh && "
        f"cd / && rm -rf {remote_extract_dir}"
    )
    return uninstall_cmd
