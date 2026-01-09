from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import msgspec
from jinja2 import Environment
from jinja2 import FileSystemLoader
from jinja2 import Template
from jinja2 import TemplateNotFound

from .errors import ImproperlyConfiguredError

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

try:
    from enum import StrEnum
except ImportError:
    from enum import Enum

    class StrEnum(str, Enum):
        pass


class InstallationMode(StrEnum):
    PY_PACKAGE = "python-package"
    BINARY = "binary"


RESERVED_PROCESS_NAMES = {"env", "caddy", "units", "socket", "timer"}


class SecretConfig(msgspec.Struct):
    adapter: str
    password_env: str | None = None

    def __post_init__(self):
        import re

        if not re.match(r"^[a-z0-9_-]+$", self.adapter):
            raise ImproperlyConfiguredError(
                f"Invalid adapter name '{self.adapter}'. "
                "Adapter names must be lowercase alphanumeric with hyphens or underscores."
            )


class TimerConfig(msgspec.Struct):
    """Configuration for systemd timer units.

    Supports various systemd timer options for flexible scheduling.
    See systemd.timer(5) for detailed documentation.
    """

    on_calendar: str | None = None
    on_boot_sec: str | None = None
    on_unit_active_sec: str | None = None
    on_active_sec: str | None = None
    persistent: bool = True
    randomized_delay_sec: str | None = None
    accuracy_sec: str | None = None

    def __post_init__(self):
        triggers = [
            self.on_calendar,
            self.on_boot_sec,
            self.on_unit_active_sec,
            self.on_active_sec,
        ]
        if not any(triggers):
            raise ImproperlyConfiguredError(
                "Timer must specify at least one trigger: on_calendar, on_boot_sec, "
                "on_unit_active_sec, or on_active_sec"
            )


class RouteConfig(msgspec.Struct):
    """Route configuration with options."""

    process: str | None = None
    static: str | None = None
    strip_prefix: str | None = None

    def __post_init__(self):
        route_types = [self.process, self.static]
        if sum(x is not None for x in route_types) != 1:
            raise ImproperlyConfiguredError(
                "Route must specify exactly one of: process, static"
            )
        if self.strip_prefix and not self.process:
            raise ImproperlyConfiguredError(
                "strip_prefix only works with process routes"
            )


class SiteConfig(msgspec.Struct):
    """Site configuration for one or more domains with routing rules."""

    domains: list[str]
    routes: dict[str, str | RouteConfig]

    def __post_init__(self):
        if not self.routes:
            raise ImproperlyConfiguredError("Site must have at least one route")


class ProcessConfig(msgspec.Struct):
    command: str
    listen: str | None = None
    replicas: int = 1
    socket: bool = False
    timer: TimerConfig | None = None

    def __post_init__(self):
        if self.socket and self.timer:
            raise ImproperlyConfiguredError(
                "A process cannot have both 'socket' and 'timer' enabled."
            )
        if self.replicas > 1 and (self.socket or self.timer):
            raise ImproperlyConfiguredError(
                "A process cannot have replicas > 1 and either 'socket' or 'timer' enabled."
            )
        if self.replicas > 1 and self.listen:
            raise ImproperlyConfiguredError(
                "A process with replicas > 1 cannot have 'listen' field. "
                "Which replica would Caddy route to?"
            )

        if self.replicas < 1:
            raise ImproperlyConfiguredError("A process must have at least 1 replica.")


class Config(msgspec.Struct, kw_only=True):
    app_name: str = msgspec.field(name="app")
    version: str = msgspec.field(default_factory=lambda: read_version_from_pyproject())
    versions_to_keep: int | None = 5
    python_version: str | None = None
    build_command: str
    release_command: str | None = None
    installation_mode: InstallationMode
    distfile: str
    aliases: dict[str, str] = msgspec.field(default_factory=dict)
    hosts: list[HostConfig]
    processes: dict[str, ProcessConfig] = msgspec.field(default_factory=dict)
    sites: list[SiteConfig] = msgspec.field(default_factory=list)
    requirements: str | None = None
    local_config_dir: Path = Path(".fujin")
    secret_config: SecretConfig | None = msgspec.field(
        name="secrets",
        default_factory=lambda: SecretConfig(adapter="system"),
    )

    def __post_init__(self):
        if not self.hosts or len(self.hosts) == 0:
            raise ImproperlyConfiguredError(
                "At least one host must be defined in 'hosts' array"
            )

        # Validate host names in multi-host setup
        if len(self.hosts) > 1:
            names = [h.name for h in self.hosts if h.name]
            if not names or len(names) != len(self.hosts):
                raise ImproperlyConfiguredError(
                    "All hosts must have a 'name' field when using multiple hosts"
                )
            if len(names) != len(set(names)):
                raise ImproperlyConfiguredError("Host names must be unique")

        if self.installation_mode == InstallationMode.PY_PACKAGE:
            if not self.python_version:
                self.python_version = find_python_version()

        if len(self.processes) == 0:
            raise ImproperlyConfiguredError("At least one process must be defined")

        for process_name in self.processes:
            if process_name.strip() == "":
                raise ImproperlyConfiguredError("Process names cannot be empty strings")
            elif process_name.count(" ") > 0:
                raise ImproperlyConfiguredError("Process names cannot contain spaces")
            elif process_name in RESERVED_PROCESS_NAMES:
                raise ImproperlyConfiguredError(
                    f"Process name '{process_name}' is reserved and cannot be used"
                )

        # Validate routes reference processes with listen
        for site in self.sites:
            for path, target in site.routes.items():
                # Extract process name from string or RouteConfig
                if isinstance(target, str):
                    process_name = target
                elif isinstance(target, RouteConfig) and target.process:
                    process_name = target.process
                else:
                    continue  # It's a static route, skip

                # Validate process exists
                if process_name not in self.processes:
                    raise ImproperlyConfiguredError(
                        f"Route '{path}' references unknown process '{process_name}'"
                    )

                # Validate process has listen (routable)
                if not self.processes[process_name].listen:
                    raise ImproperlyConfiguredError(
                        f"Process '{process_name}' must have 'listen' field to be used in routes. "
                        f"Background processes cannot be routed to."
                    )

    def select_host(self, host_name: str | None = None) -> HostConfig:
        """
        Select a host by name, or return the default (first) host.
        """
        if not host_name:
            return self.hosts[0]

        for host in self.hosts:
            if host.name == host_name:
                return host

        # Host not found - show helpful error
        available_names = [h.name for h in self.hosts if h.name]
        if available_names:
            available = ", ".join(available_names)
            raise ImproperlyConfiguredError(
                f"Host '{host_name}' not found. Available hosts: {available}"
            )
        else:
            raise ImproperlyConfiguredError(
                f"Host '{host_name}' not found. No named hosts configured."
            )

    @property
    def app_bin(self) -> str:
        if self.installation_mode == InstallationMode.PY_PACKAGE:
            return f".venv/bin/{self.app_name}"
        return self.app_name

    def app_dir(self, host: HostConfig | None = None) -> str:
        """Get app directory for the given host (or default host)."""
        host = host or self.select_host()
        return f"{host.apps_dir}/{self.app_name}"

    def get_release_dir(
        self, version: str | None = None, host: HostConfig | None = None
    ) -> str:
        """Get release directory for the given version and host."""
        return f"{self.app_dir(host)}/v{version or self.version}"

    def get_distfile_path(self, version: str | None = None) -> Path:
        version = version or self.version
        return Path(self.distfile.format(version=version))

    @classmethod
    def read(cls) -> Config:
        fujin_toml = Path("fujin.toml")
        if not fujin_toml.exists():
            raise ImproperlyConfiguredError(
                "No fujin.toml file found in the current directory"
            )
        try:
            return msgspec.toml.decode(fujin_toml.read_text(), type=cls)
        except msgspec.ValidationError as e:
            raise ImproperlyConfiguredError(f"Improperly configured, {e}") from e

    def get_unit_template_name(self, process_name: str) -> str:
        config = self.processes[process_name]
        suffix = "@.service" if config.replicas > 1 else ".service"
        if process_name == "web":
            return f"{self.app_name}{suffix}"
        return f"{self.app_name}-{process_name}{suffix}"

    def get_unit_names(self, process_name: str) -> list[str]:
        config = self.processes[process_name]
        service_name = self.get_unit_template_name(process_name)
        if config.replicas > 1:
            base = service_name.replace("@.service", "")
            return [f"{base}@{i}.service" for i in range(1, config.replicas + 1)]
        return [service_name]

    @property
    def systemd_units(self) -> list[str]:
        services = []
        for name in self.processes:
            services.extend(self.get_unit_names(name))
        for name, config in self.processes.items():
            if config.socket:
                services.append(f"{self.app_name}.socket")
            if config.timer:
                service_name = self.get_unit_template_name(name)
                services.append(f"{service_name.replace('.service', '')}.timer")
        return services

    def _template_env(self) -> Environment:
        package_templates = self._package_templates_path()
        search_paths = [self.local_config_dir, package_templates]
        return Environment(loader=FileSystemLoader(search_paths))

    def _package_templates_path(self) -> Path:
        return Path(importlib.util.find_spec("fujin").origin).parent / "templates"

    def render_systemd_units(
        self, host: HostConfig | None = None
    ) -> tuple[dict[str, str], list[str]]:
        """Render systemd units for the given host (or default host)."""
        host = host or self.select_host()
        env = self._template_env()
        package_templates = self._package_templates_path()

        context = {
            "app_name": self.app_name,
            "user": host.user,
            "app_dir": self.app_dir(host),
        }

        files = {}
        user_template_units = []

        def _get_template(name: str, default: str) -> tuple[Template, bool]:
            try:
                template = env.get_template(name)
                is_user_template = Path(template.filename).parent != package_templates
                return template, is_user_template
            except TemplateNotFound:
                template = env.get_template(default)
                return template, False

        for name, config in self.processes.items():
            service_name = self.get_unit_template_name(name)
            process_name = service_name.replace(".service", "")
            command = config.command
            process_config = config

            template, is_user_template = _get_template(
                f"{name}.service.j2", "default.service.j2"
            )
            if is_user_template:
                user_template_units.append(service_name)

            body = template.render(
                **context,
                command=command,
                process_name=process_name,
                process=process_config,
            )
            files[service_name] = body

            if process_config.socket:
                socket_name = f"{self.app_name}.socket"
                template, is_user_template = _get_template(
                    f"{name}.socket.j2", "default.socket.j2"
                )
                if is_user_template:
                    user_template_units.append(socket_name)

                body = template.render(**context)
                files[socket_name] = body

            if process_config.timer:
                timer_name = f"{service_name.replace('.service', '')}.timer"
                template, is_user_template = _get_template(
                    f"{name}.timer.j2", "default.timer.j2"
                )
                if is_user_template:
                    user_template_units.append(timer_name)

                body = template.render(
                    **context,
                    process_name=process_name,
                    process=process_config,
                )
                files[timer_name] = body

        return files, user_template_units

    def render_caddyfile(self, host: HostConfig | None = None) -> str:
        """Render Caddyfile for the given host (or default host)."""
        host = host or self.select_host()
        env = self._template_env()
        template = env.get_template("Caddyfile.j2")
        context = {"user": host.user, "app_dir": self.app_dir(host)}

        return template.render(
            sites=self.sites,
            processes=self.processes,
            **context,
        )

    @property
    def caddy_config_dir(self) -> str:
        """Get Caddy config directory. Defaults to /etc/caddy/conf.d"""
        return "/etc/caddy/conf.d"

    @property
    def caddy_config_path(self) -> str:
        return f"{self.caddy_config_dir}/{self.app_name}.caddy"


class HostConfig(msgspec.Struct, kw_only=True):
    name: str | None = None
    address: str
    user: str
    _env_file: str | None = msgspec.field(name="envfile", default=None)
    env_content: str = msgspec.field(name="env", default="")
    apps_dir: str = ".local/share/fujin"
    password_env: str | None = None
    port: int = 22
    _key_filename: str | None = msgspec.field(name="key_filename", default=None)
    key_passphrase_env: str | None = None

    def __post_init__(self):
        if self._env_file and self.env_content:
            raise ImproperlyConfiguredError(
                "Cannot set both 'env' and 'envfile' properties."
            )
        if self._env_file:
            envfile = Path(self._env_file)
            if not envfile.exists():
                raise ImproperlyConfiguredError(f"{self._env_file} not found")
            self.env_content = envfile.read_text()
        self.env_content = self.env_content.strip()
        # Only prepend /home/{user} if apps_dir is a relative path
        if not self.apps_dir.startswith("/"):
            self.apps_dir = f"/home/{self.user}/{self.apps_dir}"

    @property
    def key_filename(self) -> Path | None:
        if self._key_filename:
            return Path(self._key_filename)

    @property
    def password(self) -> str | None:
        if not self.password_env:
            return
        password = os.getenv(self.password_env)
        if password is None:
            msg = f"Env {self.password_env} can not be found"
            raise ImproperlyConfiguredError(msg)
        return password

    @property
    def key_passphrase(self) -> str | None:
        if not self.key_passphrase_env:
            return None
        value = os.getenv(self.key_passphrase_env)
        if value is None:
            raise ImproperlyConfiguredError(
                f"Env {self.key_passphrase_env} can not be found"
            )
        return value


def read_version_from_pyproject():
    try:
        return tomllib.loads(Path("pyproject.toml").read_text())["project"]["version"]
    except (FileNotFoundError, KeyError) as e:
        raise msgspec.ValidationError(
            "Project version was not found in the pyproject.toml file, define it manually"
        ) from e


def find_python_version():
    py_version_file = Path(".python-version")
    if not py_version_file.exists():
        raise msgspec.ValidationError(
            f"Add a python_version key or a .python-version file"
        )
    return py_version_file.read_text().strip()
