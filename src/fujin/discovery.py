"""Service discovery from .fujin/systemd/ directory."""

from __future__ import annotations

import configparser
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ServiceUnit:
    """Represents a discovered systemd service unit."""

    name: str  # Service name without extension (e.g., "web")
    is_template: bool  # Whether it's a template unit (@)
    service_file: Path  # Path to .service file
    socket_file: Path | None = None  # Path to .socket file if exists
    timer_file: Path | None = None  # Path to .timer file if exists


@dataclass
class DropinFile:
    """Represents a dropin configuration file."""

    name: str  # Filename
    path: Path  # Full path to dropin file


class ServiceDiscoveryError(Exception):
    """Raised when service discovery fails."""


def discover_services(fujin_dir: Path) -> list[ServiceUnit]:
    """
    Discover systemd service units from .fujin/systemd/ directory.

    Args:
        fujin_dir: Path to .fujin directory

    Returns:
        List of discovered service units

    Raises:
        ServiceDiscoveryError: If discovery fails or files are malformed
    """
    systemd_dir = fujin_dir / "systemd"

    if not systemd_dir.exists():
        return []

    services = []
    service_files = list(systemd_dir.glob("*.service"))

    for service_file in service_files:
        # Skip files in subdirectories (like service.d/)
        if service_file.parent != systemd_dir:
            continue

        # Validate the service file is parseable
        _validate_unit_file(service_file)

        # Parse filename to extract service name and template status
        filename = service_file.name
        name, is_template = _parse_service_filename(filename)

        # Look for associated socket and timer files
        socket_file = systemd_dir / f"{name}.socket"
        if is_template:
            socket_file = systemd_dir / f"{name}@.socket"

        timer_file = systemd_dir / f"{name}.timer"
        if is_template:
            timer_file = systemd_dir / f"{name}@.timer"

        # Validate associated files if they exist
        if socket_file.exists():
            _validate_unit_file(socket_file)
        else:
            socket_file = None

        if timer_file.exists():
            _validate_unit_file(timer_file)
        else:
            timer_file = None

        services.append(
            ServiceUnit(
                name=name,
                is_template=is_template,
                service_file=service_file,
                socket_file=socket_file,
                timer_file=timer_file,
            )
        )

    return sorted(services, key=lambda s: s.name)


def discover_common_dropins(fujin_dir: Path) -> list[DropinFile]:
    """
    Discover common dropin files from .fujin/systemd/common.d/

    Args:
        fujin_dir: Path to .fujin directory

    Returns:
        List of discovered dropin files

    Raises:
        ServiceDiscoveryError: If dropin files are malformed
    """
    common_dir = fujin_dir / "systemd" / "common.d"

    if not common_dir.exists():
        return []

    dropins = []
    for dropin_file in common_dir.glob("*.conf"):
        # Validate dropin is parseable
        _validate_unit_file(dropin_file)

        dropins.append(DropinFile(name=dropin_file.name, path=dropin_file))

    return sorted(dropins, key=lambda d: d.name)


def discover_service_dropins(
    fujin_dir: Path, service_name: str, is_template: bool
) -> list[DropinFile]:
    """
    Discover service-specific dropin files.

    Args:
        fujin_dir: Path to .fujin directory
        service_name: Service name (e.g., "web")
        is_template: Whether service is a template unit

    Returns:
        List of discovered dropin files

    Raises:
        ServiceDiscoveryError: If dropin files are malformed
    """
    # Build the dropin directory name
    if is_template:
        dropin_dir_name = f"{service_name}@.service.d"
    else:
        dropin_dir_name = f"{service_name}.service.d"

    dropin_dir = fujin_dir / "systemd" / dropin_dir_name

    if not dropin_dir.exists():
        return []

    dropins = []
    for dropin_file in dropin_dir.glob("*.conf"):
        # Validate dropin is parseable
        _validate_unit_file(dropin_file)

        dropins.append(DropinFile(name=dropin_file.name, path=dropin_file))

    return sorted(dropins, key=lambda d: d.name)


def _parse_service_filename(filename: str) -> tuple[str, bool]:
    """
    Parse service filename to extract name and template status.

    Args:
        filename: Service filename (e.g., "web@.service")

    Returns:
        Tuple of (service_name, is_template)
    """
    # Remove .service extension
    name = filename.removesuffix(".service")

    # Check if it's a template (ends with @)
    is_template = name.endswith("@")

    if is_template:
        name = name.removesuffix("@")

    return name, is_template


def _validate_unit_file(file_path: Path) -> None:
    """
    Validate that a systemd unit file is parseable.

    Args:
        file_path: Path to unit file

    Raises:
        ServiceDiscoveryError: If file is malformed
    """
    try:
        parser = configparser.ConfigParser(strict=False, allow_no_value=True)
        # Read as string to avoid encoding issues
        content = file_path.read_text(encoding="utf-8")
        parser.read_string(content, source=str(file_path))
    except Exception as e:
        raise ServiceDiscoveryError(f"Failed to parse {file_path.name}: {e}") from e
