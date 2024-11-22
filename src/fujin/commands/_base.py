import importlib
from contextlib import contextmanager
from dataclasses import dataclass
from functools import cached_property

import cappa

from fujin.config import Config
from fujin.connection import Connection
from fujin.connection import host_connection
from fujin.errors import ImproperlyConfiguredError
from fujin.hooks import HookManager
from fujin.process_managers import ProcessManager
from fujin.proxies import WebProxy


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

    @cached_property
    def app_dir(self) -> str:
        return self.config.host.get_app_dir(app_name=self.config.app_name)

    @contextmanager
    def connection(self):
        with host_connection(host=self.config.host) as conn:
            yield conn

    @contextmanager
    def app_environment(self) -> Connection:
        with self.connection() as conn:
            with conn.cd(self.app_dir):
                with conn.prefix("source .appenv"):
                    yield conn

    @cached_property
    def web_proxy_class(self) -> type[WebProxy]:
        module = importlib.import_module(self.config.webserver.type)
        try:
            return getattr(module, "WebProxy")
        except KeyError as e:
            raise ImproperlyConfiguredError(
                f"Missing WebProxy class in {self.config.webserver.type}"
            ) from e

    def create_web_proxy(self, conn: Connection) -> WebProxy:
        return self.web_proxy_class.create(conn=conn, config=self.config)

    @cached_property
    def process_manager_class(self) -> type[ProcessManager]:
        module = importlib.import_module(self.config.process_manager)
        try:
            return getattr(module, "ProcessManager")
        except KeyError as e:
            raise ImproperlyConfiguredError(
                f"Missing ProcessManager class in {self.config.process_manager}"
            ) from e

    def create_process_manager(self, conn: Connection) -> ProcessManager:
        return self.process_manager_class.create(conn=conn, config=self.config)

    def create_hook_manager(self, conn: Connection) -> HookManager:
        return HookManager(
            conn=conn, hooks=self.config.hooks, app_name=self.config.app_name
        )
