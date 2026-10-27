from __future__ import annotations
from functools import cache, cached_property

import os
import subprocess
import re
import sys
from contextlib import suppress
from pathlib import Path

import msgspec

from fujin.discovery import DeployedUnit, discover_deployed_units

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


class SecretConfig(msgspec.Struct, forbid_unknown_fields=True):
    adapter: str
    options: dict[str, str] = msgspec.field(default_factory=dict)

    def __post_init__(self):
        if not re.match(r"^[a-z0-9_-]+$", self.adapter):
            raise ImproperlyConfiguredError(
                f"Invalid adapter name '{self.adapter}'. "
                "Adapter names must be lowercase alphanumeric with hyphens or underscores."
            )


class HooksConfig(msgspec.Struct, forbid_unknown_fields=True):
    pre_install: list[str] = msgspec.field(default_factory=list)
    post_install: list[str] = msgspec.field(default_factory=list)
    post_start: list[str] = msgspec.field(default_factory=list)


class Config(msgspec.Struct, kw_only=True, dict=True, forbid_unknown_fields=True):
    app_name: str = msgspec.field(name="app")
    app_user: str | None = None  # User to run the app as (defaults to app_name)
    version: str = msgspec.field(default_factory=lambda: read_version_from_pyproject())
    versions_to_keep: int | None = 5
    python_version: str | None = None
    build_command: str
    build_timeout: int = 300
    installation_mode: InstallationMode
    distfile: str
    aliases: dict[str, str] = msgspec.field(default_factory=dict)
    hosts: list[HostConfig]
    replicas: dict[str, int] = msgspec.field(
        default_factory=dict
    )  # Service name -> replica count (for template units)
    requirements: str | None = None

    local_config_dir: Path = Path(".fujin")
    apps_dir: str = "/opt/fujin"
    caddy_config_dir: str = "/etc/caddy/conf.d"

    secret_config: SecretConfig | None = msgspec.field(
        name="secrets",
        default_factory=lambda: SecretConfig(adapter="system"),
    )

    hooks_config: HooksConfig | None = msgspec.field(
        name="hooks",
        default=None,
    )

    def __post_init__(self):
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

    def effective_secrets(self, host: HostConfig) -> SecretConfig | None:
        """Secret config for *this* host: host override (#75) or the global."""
        return host.secrets or self.secret_config

    @property
    def app_bin(self) -> str:
        """Path (relative to ``app_dir``) of the app binary in the release tree."""
        if self.installation_mode == InstallationMode.PY_PACKAGE:
            return f"current/venv/bin/{self.app_name}"
        return f"current/bin/{self.app_name}"

    @property
    def local_version(self) -> str:
        git_commit = get_git_short_hash()
        return f"{self.version}-{git_commit}" if git_commit else self.version

    @property
    def app_dir(self) -> str:
        return f"{self.apps_dir}/{self.app_name}"

    @property
    def install_dir(self) -> str:
        """Get .install subdirectory path (deployment infrastructure)."""
        return f"{self.app_dir}/.install"

    def get_distfile_path(self, version: str | None = None) -> Path:
        version = version or self.version
        return Path(self.distfile.format(version=version))

    @classmethod
    def read(cls, host_name: str | None = None) -> Config:
        fujin_toml = Path("fujin.toml")
        if not fujin_toml.exists():
            raise ImproperlyConfiguredError(
                "No fujin.toml file found in the current directory"
            )
        try:
            raw = tomllib.loads(fujin_toml.read_text())
        except tomllib.TOMLDecodeError as e:
            raise ImproperlyConfiguredError(f"Failed to parse fujin.toml: {e}") from e

        local_config_dir = Path(raw.get("local_config_dir", ".fujin"))
        return merge_host_layers(
            raw, _read_local_overrides(local_config_dir), host_name
        )

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
    def caddy_config_path(self) -> str:
        return f"{self.caddy_config_dir}/{self.app_name}.caddy"

    @cached_property
    def deployed_units(self) -> list[DeployedUnit]:
        return discover_deployed_units(
            self.local_config_dir, self.app_name, self.replicas
        )

    @property
    def systemd_units(self) -> list[str]:
        """All systemd unit names that should be enabled/started."""
        units = []
        for du in self.deployed_units:
            units.extend(du.all_runtime_units())
        return units


def _deep_merge(base: dict, override: dict) -> dict:
    """Merge ``override`` onto ``base`` and return a new dict.

    Scalars replace, dicts deep-merge recursively, arrays and everything
    else replace. Arrays do not append.
    """
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _decode_config_dict(data: dict) -> Config:
    """Decode raw config data into a typed Config (strict, unknown keys error)."""
    try:
        return msgspec.convert(data, type=Config, strict=True)
    except msgspec.ValidationError as e:
        raise ImproperlyConfiguredError(f"Improperly configured, {e}") from e


def _read_local_overrides(local_config_dir: Path) -> dict[str, dict]:
    """Read per-host override files from ``<local_config_dir>/hosts/``."""
    hosts_dir = local_config_dir / "hosts"
    overrides: dict[str, dict] = {}
    if hosts_dir.is_dir():
        for path in sorted(hosts_dir.glob("*.toml")):
            try:
                overrides[path.stem] = tomllib.loads(path.read_text())
            except tomllib.TOMLDecodeError as e:
                raise ImproperlyConfiguredError(f"Failed to parse {path}: {e}") from e
    return overrides


def merge_host_layers(
    raw: dict, file_overrides: dict[str, dict], host_name: str | None = None
) -> Config:
    """Merge the four configuration layers and decode the typed result.

    Layers (later wins):
      1. msgspec schema defaults
      2. ``fujin.toml`` (``[[hosts]]`` list as the primary host source)
      3. ``[hosts.<name>]`` map blocks in fujin.toml (alternative map form
         for ``hosts``, keyed by host name)
      4. ``.fujin/hosts/<name>.toml`` per-machine-local override files

    ``host_name`` (or, when ``None``, the first host) selects whose layer-4
    override file is applied. Top-level keys in that file override app-level
    settings for the selected host; its ``hosts`` entry overrides that
    host's connection/behaviour fields.

    Merge semantics: scalars replace, maps deep-merge recursively, arrays
    replace. Overrides are applied per host name, so one host's override
    never leaks into another host. The merged value tree is decoded once
    into a typed :class:`Config`.

    Raises :class:`ImproperlyConfiguredError` if an override file targets a
    host name that is not defined in fujin.toml, or if ``hosts`` mixes the
    ``[[hosts]]`` list form with ``[hosts.<name>]`` map blocks.
    """
    hosts_value = raw.get("hosts")
    if isinstance(hosts_value, dict):
        # Layer 3 map form: [hosts.<name>] blocks; each entry is a host.
        bad = sorted(k for k, v in hosts_value.items() if not isinstance(v, dict))
        if bad:
            raise ImproperlyConfiguredError(
                f"Each 'hosts' entry must define a host table; "
                f"these [hosts.<name>] blocks are not tables: {', '.join(bad)}."
            )
        hosts_list = [dict(v, name=name) for name, v in hosts_value.items()]
    elif isinstance(hosts_value, list):
        for i, entry in enumerate(hosts_value):
            if not isinstance(entry, dict):
                raise ImproperlyConfiguredError(
                    f"Each 'hosts' entry must define a host table ([[hosts]]); "
                    f"entry {i} is {type(entry).__name__}, not a table."
                )
        _validate_hosts_list_form(hosts_value)
        hosts_list = hosts_value
    else:
        hosts_list = None
    if not hosts_list:
        raise ImproperlyConfiguredError(
            "At least one host must be defined in 'hosts' array"
        )

    names_in_config = [h.get("name") for h in hosts_list]
    known = {n for n in names_in_config if n}
    unknown = set(file_overrides) - known
    if unknown:
        available = ", ".join(sorted(n for n in known if n)) or "none"
        raise ImproperlyConfiguredError(
            f"Host override file(s) found for unknown host(s): "
            f"{', '.join(sorted(unknown))}. "
            f"The host must be defined in fujin.toml first (available: {available})."
        )

    if host_name and host_name not in known:
        available = ", ".join(sorted(n for n in known if n)) or "none"
        raise ImproperlyConfiguredError(
            f"Host '{host_name}' not found. Available hosts: {available}"
        )

    selected = host_name or next((n for n in names_in_config if n), None)

    # Layer 4: file override splits into host-level and app-level keys.
    # Keys that are HostConfig fields are host-level; anything else is an
    # app-level override applied for the selected host only.
    selected_file_override: dict = dict(file_overrides.get(selected, {}))
    override_hosts: dict | list | None = selected_file_override.pop("hosts", None)
    override_host_data = _extract_override_host(override_hosts, selected)
    host_keys = {
        k: v for k, v in selected_file_override.items() if k in _HOST_OVERRIDE_KEYS
    }
    app_keys = {
        k: v for k, v in selected_file_override.items() if k not in _HOST_OVERRIDE_KEYS
    }
    override_host_data = _deep_merge(host_keys, override_host_data)

    app_data = {k: v for k, v in raw.items() if k != "hosts"}
    app_data = _deep_merge(app_data, app_keys)

    merged_hosts = []
    for host in hosts_list:
        host_data = dict(host)
        if host.get("name") == selected and override_host_data:
            had_env, had_envfile = "env" in host_data, "envfile" in host_data
            host_data = _deep_merge(host_data, override_host_data)
            # An override that introduces one of env/envfile must quash the
            # BASE's counterpart key: TOML has no null, so it could never be
            # unset again and would trip the "both set" conflict check
            # forever after. Keys the override itself set are left alone.
            if (
                "env" in override_host_data
                and had_envfile
                and "envfile" not in override_host_data
            ):
                host_data.pop("envfile", None)
            elif (
                "envfile" in override_host_data
                and had_env
                and "env" not in override_host_data
            ):
                host_data.pop("env", None)
        merged_hosts.append(host_data)

    app_data["hosts"] = merged_hosts
    return _decode_config_dict(app_data)


# TOML keys of HostConfig (host-level override file fields).
_HOST_OVERRIDE_KEYS = frozenset(
    {
        "name",
        "address",
        "user",
        "envfile",
        "env",
        "port",
        "key_filename",
        "key_passphrase_env",
        "native_ssh_auth",
        "secrets",
    }
)


def _validate_hosts_list_form(hosts_value: list[dict]) -> None:
    """Detect a ``hosts`` list entry containing keys that are not host fields.

    TOML allows ``[hosts.<name>]`` map blocks after ``[[hosts]]`` entries, in
    which case the block nests into the last list entry as a table-valued
    key. That almost always means the two forms were mixed (the map block
    silently rewrites the last host otherwise); a scalar-valued unknown key
    is an unknown-field typo. Both hard-error, with distinct messages.
    """
    for entry in hosts_value:
        unknown = {k: v for k, v in entry.items() if k not in _HOST_OVERRIDE_KEYS}
        if not unknown:
            continue
        mixed = sorted(k for k, v in unknown.items() if isinstance(v, dict))
        if mixed:
            raise ImproperlyConfiguredError(
                "hosts mixes the list form [[hosts]] with the map form "
                f"[hosts.{mixed[0]}]; the block nests into the last [[hosts]] "
                "entry and silently rewrites it. Use one form: either an "
                "array of [[hosts]] tables or named [hosts.<name>] blocks."
            )
        typos = sorted(unknown)
        raise ImproperlyConfiguredError(
            f"Unknown host field(s) in hosts: {', '.join(typos)}. "
            f"Valid fields: {', '.join(sorted(_HOST_OVERRIDE_KEYS))}."
        )


def _extract_override_host(
    override_hosts: dict | list | None, selected: str | None
) -> dict:
    """Pull the selected host entry out of a layer-4 override ``hosts`` value."""
    if not override_hosts:
        return {}
    target = selected if selected else "(unnamed host)"
    if isinstance(override_hosts, dict):
        mismatched = [n for n in override_hosts if n != selected]
        if mismatched:
            raise ImproperlyConfiguredError(
                f"Override file for '{target}' may not describe other hosts: "
                f"{', '.join(sorted(mismatched))}. Each per-host override file "
                "must only contain its own host."
            )
        return next(iter(override_hosts.values()), {})
    # List form: [[hosts]] with name = "..."
    for entry in override_hosts:
        if not isinstance(entry, dict):
            raise ImproperlyConfiguredError(
                "Host override file 'hosts' entries must be TOML tables"
            )
        entry_name = entry.get("name")
        if entry_name != selected:
            name = entry_name if entry_name else "(unnamed entry)"
            raise ImproperlyConfiguredError(
                f"Override file for '{target}' may not describe other hosts: "
                f"{name}. Each per-host override file must only contain its "
                "own host."
            )
        return {k: v for k, v in entry.items() if k != "name"}
    raise ImproperlyConfiguredError(
        f"Host override file 'hosts' entries must include the host name '{selected}'"
    )


class HostConfig(msgspec.Struct, kw_only=True, forbid_unknown_fields=True):
    name: str | None = None
    address: str
    user: str
    _env_file: str | None = msgspec.field(name="envfile", default=None)
    env_content: str = msgspec.field(name="env", default="")
    port: int = 22
    _key_filename: str | None = msgspec.field(name="key_filename", default=None)
    key_passphrase_env: str | None = None
    native_ssh_auth: bool = False  # Use native ssh for auth (for Tailscale, etc.)
    #: Per-host secret adapter override (#75); None = use the global
    #: ``[secrets]`` / ``Config.secret_config``.
    secrets: SecretConfig | None = None

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
            "Add a python_version key or a .python-version file"
        )
    return py_version_file.read_text().strip()


@cache
def get_git_short_hash() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        return ""
