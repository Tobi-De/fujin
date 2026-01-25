from __future__ import annotations

import configparser
from pathlib import Path

import msgspec

from fujin.errors import ServiceDiscoveryError


class DeployedUnit(msgspec.Struct, kw_only=True):
    name: str  # Base name, e.g., "web"
    app_name: str  # e.g., "myapp"
    service_file: Path
    socket_file: Path | None = None
    timer_file: Path | None = None
    replicas: int = 1

    @property
    def is_template(self) -> bool:
        return self.replicas > 1

    def service_instances(self) -> list[str]:
        """Runtime service units for start/stop/restart/logs."""
        if self.is_template:
            return [
                f"{self.app_name}-{self.name}@{i}.service"
                for i in range(1, self.replicas + 1)
            ]
        return [f"{self.app_name}-{self.name}.service"]

    def auxiliary_units(self) -> list[str]:
        """Socket and timer units (always singletons)."""
        units = []
        if self.socket_file:
            units.append(f"{self.app_name}-{self.name}.socket")
        if self.timer_file:
            units.append(f"{self.app_name}-{self.name}.timer")
        return units

    def all_runtime_units(self) -> list[str]:
        """All units for systemctl operations."""
        return self.service_instances() + self.auxiliary_units()

    @property
    def template_service_name(self) -> str:
        """For systemctl cat/show on template definition."""
        suffix = "@" if self.is_template else ""
        return f"{self.app_name}-{self.name}{suffix}.service"

    @property
    def template_socket_name(self) -> str | None:
        """For systemctl cat/show on socket definition."""
        return f"{self.app_name}-{self.name}.socket" if self.socket_file else None

    @property
    def template_timer_name(self) -> str | None:
        """For systemctl cat/show on timer definition."""
        return f"{self.app_name}-{self.name}.timer" if self.timer_file else None


def discover_deployed_units(
    fujin_dir: Path, app_name: str, replicas: dict[str, int]
) -> list[DeployedUnit]:
    """
    Discover systemd units from .fujin/systemd/ directory.

    Args:
        fujin_dir: Path to .fujin directory
        app_name: Application name for unit naming
        replicas: Dict mapping service name to replica count
    """
    systemd_dir = fujin_dir / "systemd"

    if not systemd_dir.exists():
        return []

    result = []
    service_files = list(systemd_dir.glob("*.service"))

    for service_file in service_files:
        # Skip files in subdirectories (like service.d/)
        if service_file.parent != systemd_dir:
            continue

        _validate_unit_file(service_file)

        # Parse filename to extract service name
        # Handles both "web.service" and "web@.service"
        filename = service_file.name
        name = filename.removesuffix(".service").removesuffix("@")

        # Look for associated socket and timer files (always singletons)
        socket_path = systemd_dir / f"{name}.socket"
        timer_path = systemd_dir / f"{name}.timer"

        # Validate associated files if they exist
        socket_file: Path | None = None
        timer_file: Path | None = None

        if socket_path.exists():
            _validate_unit_file(socket_path)
            socket_file = socket_path

        if timer_path.exists():
            _validate_unit_file(timer_path)
            timer_file = timer_path

        replica_count = replicas.get(name, 1)

        result.append(
            DeployedUnit(
                name=name,
                app_name=app_name,
                service_file=service_file,
                socket_file=socket_file,
                timer_file=timer_file,
                replicas=replica_count,
            )
        )

    return sorted(result, key=lambda u: u.name)


def _validate_unit_file(file_path: Path) -> None:
    try:
        parser = configparser.ConfigParser(strict=False, allow_no_value=True)
        content = file_path.read_text(encoding="utf-8")
        parser.read_string(content, source=str(file_path))
    except Exception as e:
        raise ServiceDiscoveryError(f"Failed to parse {file_path.name}: {e}") from e
