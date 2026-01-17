"""Tests for service discovery."""

from __future__ import annotations


import pytest

from fujin.discovery import (
    discover_deployed_units,
    ServiceDiscoveryError,
)


@pytest.mark.parametrize(
    "setup_dirs",
    [
        ["systemd"],  # Empty systemd directory
        [],  # No systemd directory
    ],
)
def test_discover_services_returns_empty_when_no_services(tmp_path, setup_dirs):
    """Should return empty list if no services found or systemd dir missing."""
    install_dir = tmp_path / ".fujin"
    install_dir.mkdir()
    for dir_name in setup_dirs:
        (install_dir / dir_name).mkdir()

    units = discover_deployed_units(install_dir, "myapp", {})
    assert units == []


def test_discover_single_service(tmp_path):
    """Should discover a single service."""
    install_dir = tmp_path / ".fujin"
    systemd_dir = install_dir / "systemd"
    systemd_dir.mkdir(parents=True)

    # Create a simple service file
    service_file = systemd_dir / "web.service"
    service_file.write_text("""[Unit]
Description=Web server

[Service]
ExecStart=/bin/true

[Install]
WantedBy=multi-user.target
""")

    units = discover_deployed_units(install_dir, "myapp", {})

    assert len(units) == 1
    assert units[0].service_name == "web"
    assert units[0].is_template is False
    assert units[0].service_file == service_file
    assert units[0].socket_file is None
    assert units[0].timer_file is None
    assert units[0].template_service_name == "myapp-web.service"
    assert units[0].replica_count == 1
    assert units[0].instance_service_names == ["myapp-web.service"]


def test_discover_template_service(tmp_path):
    """Should discover a template service (with @)."""
    install_dir = tmp_path / ".fujin"
    systemd_dir = install_dir / "systemd"
    systemd_dir.mkdir(parents=True)

    service_file = systemd_dir / "web@.service"
    service_file.write_text("""[Unit]
Description=Web server %i

[Service]
ExecStart=/bin/true

[Install]
WantedBy=multi-user.target
""")

    units = discover_deployed_units(install_dir, "myapp", {"web": 3})

    assert len(units) == 1
    assert units[0].service_name == "web"
    assert units[0].is_template is True
    assert units[0].service_file == service_file
    assert units[0].template_service_name == "myapp-web@.service"
    assert units[0].replica_count == 3
    assert units[0].instance_service_names == [
        "myapp-web@1.service",
        "myapp-web@2.service",
        "myapp-web@3.service",
    ]


def test_discover_service_with_socket_and_replicas(tmp_path):
    """Should discover service with socket for both single and multi-replica configs."""
    install_dir = tmp_path / ".fujin"
    systemd_dir = install_dir / "systemd"
    systemd_dir.mkdir(parents=True)

    # Test single replica (web.service + web.socket)
    service_file = systemd_dir / "web.service"
    service_file.write_text("""[Unit]
Description=Web

[Service]
ExecStart=/bin/true

[Install]
WantedBy=multi-user.target
""")

    socket_file = systemd_dir / "web.socket"
    socket_file.write_text("""[Unit]
Description=Web socket

[Socket]
ListenStream=/run/web.sock

[Install]
WantedBy=sockets.target
""")

    units = discover_deployed_units(install_dir, "myapp", {})
    assert len(units) == 1
    assert units[0].socket_file == socket_file
    assert units[0].template_socket_name == "myapp-web.socket"

    # Clean up for template test
    service_file.unlink()
    socket_file.unlink()

    # Test multi-replica (web@.service + web@.socket)
    template_service = systemd_dir / "web@.service"
    template_service.write_text("""[Unit]
Description=Web %i

[Service]
ExecStart=/bin/true

[Install]
WantedBy=multi-user.target
""")

    template_socket = systemd_dir / "web@.socket"
    template_socket.write_text("""[Unit]
Description=Web socket %i

[Socket]
ListenStream=/run/web-%i.sock

[Install]
WantedBy=sockets.target
""")

    units = discover_deployed_units(install_dir, "myapp", {})
    assert len(units) == 1
    assert units[0].socket_file == template_socket
    assert units[0].template_socket_name == "myapp-web@.socket"


def test_discover_service_with_timer(tmp_path):
    """Should discover service and associated timer file."""
    install_dir = tmp_path / ".fujin"
    systemd_dir = install_dir / "systemd"
    systemd_dir.mkdir(parents=True)

    service_file = systemd_dir / "cleanup.service"
    service_file.write_text("""[Unit]
Description=Cleanup

[Service]
Type=oneshot
ExecStart=/bin/true
""")

    timer_file = systemd_dir / "cleanup.timer"
    timer_file.write_text("""[Unit]
Description=Cleanup timer

[Timer]
OnCalendar=daily

[Install]
WantedBy=timers.target
""")

    units = discover_deployed_units(install_dir, "myapp", {})

    assert len(units) == 1
    assert units[0].timer_file == timer_file
    assert units[0].template_timer_name == "myapp-cleanup.timer"


def test_discover_multiple_services(tmp_path):
    """Should discover multiple services."""
    install_dir = tmp_path / ".fujin"
    systemd_dir = install_dir / "systemd"
    systemd_dir.mkdir(parents=True)

    for name in ["web", "worker", "cleanup"]:
        (systemd_dir / f"{name}.service").write_text("""[Unit]
Description=Service

[Service]
ExecStart=/bin/true

[Install]
WantedBy=multi-user.target
""")

    units = discover_deployed_units(install_dir, "myapp", {})

    assert len(units) == 3
    names = [u.service_name for u in units]
    assert sorted(names) == ["cleanup", "web", "worker"]


def test_discover_services_fails_on_malformed_file(tmp_path):
    """Should fail with clear error on malformed service file."""
    install_dir = tmp_path / ".fujin"
    systemd_dir = install_dir / "systemd"
    systemd_dir.mkdir(parents=True)

    # Create malformed file (invalid INI)
    service_file = systemd_dir / "web.service"
    service_file.write_text("This is not valid INI\n[[[broken")

    with pytest.raises(ServiceDiscoveryError) as exc_info:
        discover_deployed_units(install_dir, "myapp", {})

    assert "Failed to parse web.service" in exc_info.value.message
