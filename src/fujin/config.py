from __future__ import annotations
from contextlib import suppress
import re

from fujin.discovery import discover_deployed_units, DeployedUnit
import os
import sys
from pathlib import Path

import msgspec

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


class SecretConfig(msgspec.Struct):
    adapter: str
    password_env: str | None = None

    def __post_init__(self):
        if not re.match(r"^[a-z0-9_-]+$", self.adapter):
            raise ImproperlyConfiguredError(
                f"Invalid adapter name '{self.adapter}'. "
                "Adapter names must be lowercase alphanumeric with hyphens or underscores."
            )


class Config(msgspec.Struct, kw_only=True):
    app_name: str = msgspec.field(name="app")
    app_user: str | None = None  # User to run the app as (defaults to app_name)
    version: str = msgspec.field(default_factory=lambda: read_version_from_pyproject())
    versions_to_keep: int | None = 5
    python_version: str | None = None
    build_command: str
    installation_mode: InstallationMode
    distfile: str
    aliases: dict[str, str] = msgspec.field(default_factory=dict)
    hosts: list[HostConfig]
    replicas: dict[str, int] = msgspec.field(
        default_factory=dict
    )  # Service name -> replica count (for template units)
    requirements: str | None = None
    local_config_dir: Path = Path(".fujin")
    secret_config: SecretConfig | None = msgspec.field(
        name="secrets",
        default_factory=lambda: SecretConfig(adapter="system"),
    )

    def __post_init__(self):
        # Default app_user to app_name if not specified
        if not self.app_user:
            self.app_user = self.app_name

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
            return f".install/.venv/bin/{self.app_name}"
        return f".install/{self.app_name}"

    @property
    def apps_dir(self) -> str:
        """Apps are always deployed to /opt/fujin."""
        return "/opt/fujin"

    @property
    def app_dir(self) -> str:
        """Get app directory."""
        return f"{self.apps_dir}/{self.app_name}"

    @property
    def install_dir(self) -> str:
        """Get .install subdirectory path (deployment infrastructure)."""
        return f"{self.app_dir}/.install"

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

    @property
    def caddyfile_path(self) -> Path:
        return self.local_config_dir / "Caddyfile"

    @property
    def caddyfile_exists(self) -> bool:
        return self.caddyfile_path.exists()

    def get_domain_name(self) -> str | None:
        if not self.caddyfile_exists:
            return None
        with suppress(OSError, UnicodeError):
            content = self.caddyfile_path.read_text()
            # Look for domain pattern: domain.com {
            # Match lines that look like domain blocks (simple heuristic)
            for line in content.splitlines():
                line = line.strip()
                # Skip comments and empty lines
                if not line or line.startswith("#"):
                    continue
                # Look for pattern: something.something {
                if "{" in line and not line.startswith("handle"):
                    # Extract domain (before the {)
                    # Handle multiple domains: "example.com, www.example.com {" -> "example.com"
                    domain_part = line.split("{")[0]
                    domain = domain_part.split(",")[0].strip()

                    # Basic validation: has a dot and doesn't start with special chars
                    if "." in domain and not domain.startswith(("@", "*", "http")):
                        return domain

    @property
    def caddy_config_dir(self) -> str:
        """Get Caddy config directory. Defaults to /etc/caddy/conf.d"""
        return "/etc/caddy/conf.d"

    @property
    def caddy_config_path(self) -> str:
        return f"{self.caddy_config_dir}/{self.app_name}.caddy"

    @property
    def deployed_units(self) -> list[DeployedUnit]:
        return discover_deployed_units(
            self.local_config_dir, self.app_name, self.replicas
        )

    @property
    def systemd_units(self) -> list[str]:
        """All systemd unit names that should be enabled/started."""
        units = []
        for du in self.deployed_units:
            units.extend(du.all_unit_names())
        return units


class HostConfig(msgspec.Struct, kw_only=True):
    name: str | None = None
    address: str
    user: str
    _env_file: str | None = msgspec.field(name="envfile", default=None)
    env_content: str = msgspec.field(name="env", default="")
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
