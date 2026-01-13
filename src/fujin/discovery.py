from __future__ import annotations
from fujin.errors import ServiceDiscoveryError

import configparser
from pathlib import Path
import msgspec


class DeployedUnit(msgspec.Struct, kw_only=True):
    """
    Complete information about a deployed systemd unit.
    Single source of truth combining discovery metadata with deployment context.
    """

    # From discovery
    service_name: str  # e.g., "web"
    is_template: bool
    service_file: Path
    socket_file: Path | None
    timer_file: Path | None

    # Template names (for systemctl cat/show, installer metadata)
    template_service_name: str  # e.g., "myapp-web@.service" or "myapp-web.service"
    template_socket_name: str | None  # e.g., "myapp-web@.socket" or "myapp-web.socket"
    template_timer_name: str | None  # e.g., "myapp-web@.timer" or "myapp-web.timer"

    # Instance information (for operations: start/stop/restart/logs)
    replica_count: int  # From config.replicas, default 1
    instance_service_names: list[str]  # ["myapp-web@1.service", "myapp-web@2.service"]

    def all_unit_names(self) -> list[str]:
        """All unit names that should be enabled/started (instances + socket/timer)."""
        units = self.instance_service_names.copy()
        # For templates, socket/timer don't have instances
        if not self.is_template:
            if self.template_socket_name:
                units.append(self.template_socket_name)
            if self.template_timer_name:
                units.append(self.template_timer_name)
        else:
            # Template sockets/timers use @ without instance number
            if self.template_socket_name:
                units.append(self.template_socket_name)
            if self.template_timer_name:
                units.append(self.template_timer_name)
        return units


def discover_deployed_units(
    fujin_dir: Path, app_name: str, replicas: dict[str, int]
) -> list[DeployedUnit]:
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

        # Parse filename to extract service name and template status
        filename = service_file.name
        name = filename.removesuffix(".service")
        is_template = name.endswith("@")
        if is_template:
            name = name.removesuffix("@")

        # Look for associated socket and timer files
        socket_file = (
            systemd_dir / f"{name}@.socket"
            if is_template
            else systemd_dir / f"{name}.socket"
        )
        timer_file = (
            systemd_dir / f"{name}@.timer"
            if is_template
            else systemd_dir / f"{name}.timer"
        )

        # Validate associated files if they exist
        if socket_file.exists():
            _validate_unit_file(socket_file)
        else:
            socket_file = None

        if timer_file.exists():
            _validate_unit_file(timer_file)
        else:
            timer_file = None

        # Get replica count for this service
        replica_count = replicas.get(name, 1)

        # Build template names (for cat/show commands and installer)
        suffix = "@" if is_template else ""
        template_service = f"{app_name}-{name}{suffix}.service"
        template_socket = f"{app_name}-{name}{suffix}.socket" if socket_file else None
        template_timer = f"{app_name}-{name}{suffix}.timer" if timer_file else None

        # Build instance names (for start/stop/restart/logs)
        if is_template:
            instance_services = [
                f"{app_name}-{name}@{i}.service" for i in range(1, replica_count + 1)
            ]
        else:
            instance_services = [template_service]

        result.append(
            DeployedUnit(
                service_name=name,
                is_template=is_template,
                service_file=service_file,
                socket_file=socket_file,
                timer_file=timer_file,
                template_service_name=template_service,
                template_socket_name=template_socket,
                template_timer_name=template_timer,
                replica_count=replica_count,
                instance_service_names=instance_services,
            )
        )

    return sorted(result, key=lambda u: u.service_name)


def _validate_unit_file(file_path: Path) -> None:
    try:
        parser = configparser.ConfigParser(strict=False, allow_no_value=True)
        # Read as string to avoid encoding issues
        content = file_path.read_text(encoding="utf-8")
        parser.read_string(content, source=str(file_path))
    except Exception as e:
        raise ServiceDiscoveryError(f"Failed to parse {file_path.name}: {e}") from e
