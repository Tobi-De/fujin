from __future__ import annotations

import importlib.util
import os
import sys
import shlex
from pathlib import Path

import msgspec
from jinja2 import Environment, FileSystemLoader, Template, TemplateNotFound

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


class SecretAdapter(StrEnum):
    BITWARDEN = "bitwarden"
    ONE_PASSWORD = "1password"
    DOPPLER = "doppler"
    SYSTEM = "system"


class SecretConfig(msgspec.Struct):
    adapter: SecretAdapter
    password_env: str | None = None


class ProcessConfig(msgspec.Struct):
    command: str
    replicas: int = 1
    socket: bool = False
    timer: str | None = None
    context: dict[str, str] = msgspec.field(default_factory=dict)

    def __post_init__(self):
        if self.socket and self.timer:
            raise ImproperlyConfiguredError(
                "A process cannot have both 'socket' and 'timer' enabled."
            )
        if self.replicas > 1 and (self.socket or self.timer):
            raise ImproperlyConfiguredError(
                "A process cannot have replicas > 1 and either 'socket' or 'timer' enabled."
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
    host: HostConfig
    processes: dict[str, ProcessConfig] = msgspec.field(default_factory=dict)
    webserver: Webserver
    requirements: str | None = None
    local_config_dir: Path = Path(".fujin")
    secret_config: SecretConfig | None = msgspec.field(
        name="secrets",
        default_factory=lambda: SecretConfig(adapter=SecretAdapter.SYSTEM),
    )

    def __post_init__(self):
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

        if "web" not in self.processes and self.webserver.enabled:
            raise ImproperlyConfiguredError(
                "Missing web process or set the proxy enabled to False to disable the use of a proxy"
            )

    @property
    def app_bin(self) -> str:
        if self.installation_mode == InstallationMode.PY_PACKAGE:
            return f".venv/bin/{self.app_name}"
        return self.app_name

    @property
    def app_dir(self) -> str:
        return f"{self.host.apps_dir}/{self.app_name}"

    def get_release_dir(self, version: str | None = None) -> str:
        return f"{self.app_dir}/v{version or self.version}"

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

    def get_active_unit_names(self, process_name: str) -> list[str]:
        config = self.processes[process_name]
        service_name = self.get_unit_template_name(process_name)
        if config.replicas > 1:
            base = service_name.replace("@.service", "")
            return [f"{base}@{i}.service" for i in range(1, config.replicas + 1)]
        return [service_name]

    @property
    def active_systemd_units(self) -> list[str]:
        services = []
        for name in self.processes:
            services.extend(self.get_active_unit_names(name))
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

    def render_systemd_units(self) -> tuple[dict[str, str], list[str]]:
        env = self._template_env()
        package_templates = self._package_templates_path()

        context = {
            "app_name": self.app_name,
            "user": self.host.user,
            "app_dir": self.app_dir,
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
                context=process_config.context,
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

                body = template.render(context=process_config.context, **context)
                files[socket_name] = body

            if process_config.timer:
                timer_name = f"{service_name.replace('.service', '')}.timer"
                template, is_user_template = _get_template(
                    f"{name}.timer.j2", "default.timer.j2"
                )
                if is_user_template:
                    user_template_units.append(timer_name)

                body = template.render(
                    context=process_config.context,
                    **context,
                    process_name=process_name,
                    process=process_config,
                )
                files[timer_name] = body

        return files, user_template_units

    def render_caddyfile(self) -> str:
        env = self._template_env()
        template = env.get_template("Caddyfile.j2")
        return template.render(
            domain_name=self.host.domain_name,
            upstream=self.webserver.upstream,
            statics=self.webserver.statics,
        )

    def build_context(
        self,
        *,
        distfile_name: str,
        user_units: list[str],
        new_units: dict[str, str],
    ) -> dict:
        units_valid = sorted(set(self.active_systemd_units) | set(new_units.keys()))
        regular_units = [
            u for u in self.active_systemd_units if not u.endswith("@.service")
        ]
        template_units = [
            u for u in self.active_systemd_units if u.endswith("@.service")
        ]

        def to_bash_array(values: list[str]) -> str:
            return "(" + " ".join(shlex.quote(v) for v in values) + ")"

        return {
            "app_name": self.app_name,
            "app_dir": self.app_dir,
            "version": self.version,
            "installation_mode": self.installation_mode.value,
            "python_version": self.python_version,
            "requirements": bool(self.requirements),
            "distfile_name": distfile_name,
            "release_command": self.release_command,
            "webserver_enabled": self.webserver.enabled,
            "caddy_config_path": self.caddy_config_path,
            "app_bin": self.app_bin,
            "units": {
                "active": to_bash_array(self.active_systemd_units),
                "valid": to_bash_array(units_valid),
                "regular": to_bash_array(regular_units),
                "regular_len": len(regular_units),
                "template": to_bash_array(template_units),
                "template_len": len(template_units),
                "user": to_bash_array(user_units),
                "user_len": len(user_units),
            },
        }

    def render_install_script(self, *, context: dict) -> str:
        env = self._template_env()
        template = env.get_template("install.sh.j2")
        return template.render(**context)

    def render_uninstall_script(self, *, context: dict) -> str:
        env = self._template_env()
        template = env.get_template("uninstall.sh.j2")
        return template.render(**context)

    @property
    def caddy_config_path(self) -> str:
        return f"{self.webserver.config_dir}/{self.app_name}.caddy"


class HostConfig(msgspec.Struct, kw_only=True):
    ip: str | None = None
    domain_name: str
    user: str
    _env_file: str | None = msgspec.field(name="envfile", default=None)
    env_content: str | None = msgspec.field(name="env", default=None)
    apps_dir: str = ".local/share/fujin"
    password_env: str | None = None
    ssh_port: int = 22
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
        self.env_content = self.env_content.strip() if self.env_content else ""
        self.apps_dir = f"/home/{self.user}/{self.apps_dir}"
        self.ip = self.ip or self.domain_name

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


class Webserver(msgspec.Struct):
    upstream: str
    enabled: bool = True
    statics: dict[str, str] = msgspec.field(default_factory=dict)
    config_dir: str = "/etc/caddy/conf.d"


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
