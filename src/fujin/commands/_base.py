from contextlib import contextmanager
from dataclasses import dataclass
from functools import cached_property
from typing import Generator

import cappa

from fujin.config import Config
from fujin.libssh_connection import SSH2Connection
from fujin.libssh_connection import connection as host_connection


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
    def connection(self):
        with host_connection(host=self.config.host) as conn:
            yield conn

    @contextmanager
    def app_environment(self) -> Generator[SSH2Connection, None, None]:
        with self.connection() as conn:
            with conn.cd(self.config.app_dir):
                with conn.prefix("source .appenv"):
                    yield conn
