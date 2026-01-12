"""Tests for service discovery."""

from __future__ import annotations


import pytest

from fujin.discovery import (
    discover_common_dropins,
    discover_service_dropins,
    discover_services,
    ServiceDiscoveryError,
)


def test_discover_services_empty_directory(tmp_path):
    """Should return empty list if no services found."""
    fujin_dir = tmp_path / ".fujin"
    fujin_dir.mkdir()
    (fujin_dir / "systemd").mkdir()

    services = discover_services(fujin_dir)
    assert services == []


def test_discover_services_no_systemd_directory(tmp_path):
    """Should return empty list if systemd directory doesn't exist."""
    fujin_dir = tmp_path / ".fujin"
    fujin_dir.mkdir()

    services = discover_services(fujin_dir)
    assert services == []


def test_discover_single_service(tmp_path):
    """Should discover a single service."""
    fujin_dir = tmp_path / ".fujin"
    systemd_dir = fujin_dir / "systemd"
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

    services = discover_services(fujin_dir)

    assert len(services) == 1
    assert services[0].name == "web"
    assert services[0].is_template is False
    assert services[0].service_file == service_file
    assert services[0].socket_file is None
    assert services[0].timer_file is None


def test_discover_template_service(tmp_path):
    """Should discover a template service (with @)."""
    fujin_dir = tmp_path / ".fujin"
    systemd_dir = fujin_dir / "systemd"
    systemd_dir.mkdir(parents=True)

    service_file = systemd_dir / "web@.service"
    service_file.write_text("""[Unit]
Description=Web server %i

[Service]
ExecStart=/bin/true

[Install]
WantedBy=multi-user.target
""")

    services = discover_services(fujin_dir)

    assert len(services) == 1
    assert services[0].name == "web"
    assert services[0].is_template is True
    assert services[0].service_file == service_file


def test_discover_service_with_socket(tmp_path):
    """Should discover service and associated socket file."""
    fujin_dir = tmp_path / ".fujin"
    systemd_dir = fujin_dir / "systemd"
    systemd_dir.mkdir(parents=True)

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

    services = discover_services(fujin_dir)

    assert len(services) == 1
    assert services[0].socket_file == socket_file


def test_discover_template_service_with_socket(tmp_path):
    """Should discover template service with template socket."""
    fujin_dir = tmp_path / ".fujin"
    systemd_dir = fujin_dir / "systemd"
    systemd_dir.mkdir(parents=True)

    service_file = systemd_dir / "web@.service"
    service_file.write_text("""[Unit]
Description=Web %i

[Service]
ExecStart=/bin/true

[Install]
WantedBy=multi-user.target
""")

    socket_file = systemd_dir / "web@.socket"
    socket_file.write_text("""[Unit]
Description=Web socket %i

[Socket]
ListenStream=/run/web-%i.sock

[Install]
WantedBy=sockets.target
""")

    services = discover_services(fujin_dir)

    assert len(services) == 1
    assert services[0].socket_file == socket_file


def test_discover_service_with_timer(tmp_path):
    """Should discover service and associated timer file."""
    fujin_dir = tmp_path / ".fujin"
    systemd_dir = fujin_dir / "systemd"
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

    services = discover_services(fujin_dir)

    assert len(services) == 1
    assert services[0].timer_file == timer_file


def test_discover_multiple_services(tmp_path):
    """Should discover multiple services."""
    fujin_dir = tmp_path / ".fujin"
    systemd_dir = fujin_dir / "systemd"
    systemd_dir.mkdir(parents=True)

    for name in ["web", "worker", "cleanup"]:
        (systemd_dir / f"{name}.service").write_text("""[Unit]
Description=Service

[Service]
ExecStart=/bin/true

[Install]
WantedBy=multi-user.target
""")

    services = discover_services(fujin_dir)

    assert len(services) == 3
    names = [s.name for s in services]
    assert sorted(names) == ["cleanup", "web", "worker"]


def test_discover_services_fails_on_malformed_file(tmp_path):
    """Should fail with clear error on malformed service file."""
    fujin_dir = tmp_path / ".fujin"
    systemd_dir = fujin_dir / "systemd"
    systemd_dir.mkdir(parents=True)

    # Create malformed file (invalid INI)
    service_file = systemd_dir / "web.service"
    service_file.write_text("This is not valid INI\n[[[broken")

    with pytest.raises(ServiceDiscoveryError) as exc_info:
        discover_services(fujin_dir)

    assert "Failed to parse web.service" in str(exc_info.value)


def test_discover_common_dropins_empty(tmp_path):
    """Should return empty list if no dropins."""
    fujin_dir = tmp_path / ".fujin"
    (fujin_dir / "systemd" / "common.d").mkdir(parents=True)

    dropins = discover_common_dropins(fujin_dir)
    assert dropins == []


def test_discover_common_dropins(tmp_path):
    """Should discover common dropin files."""
    fujin_dir = tmp_path / ".fujin"
    common_dir = fujin_dir / "systemd" / "common.d"
    common_dir.mkdir(parents=True)

    # Create dropin files
    (common_dir / "base.conf").write_text("""[Service]
User=deploy
""")

    (common_dir / "security.conf").write_text("""[Service]
NoNewPrivileges=true
""")

    dropins = discover_common_dropins(fujin_dir)

    assert len(dropins) == 2
    names = [d.name for d in dropins]
    assert sorted(names) == ["base.conf", "security.conf"]


def test_discover_common_dropins_no_directory(tmp_path):
    """Should return empty list if common.d doesn't exist."""
    fujin_dir = tmp_path / ".fujin"
    fujin_dir.mkdir()

    dropins = discover_common_dropins(fujin_dir)
    assert dropins == []


def test_discover_common_dropins_fails_on_malformed(tmp_path):
    """Should fail on malformed dropin file."""
    fujin_dir = tmp_path / ".fujin"
    common_dir = fujin_dir / "systemd" / "common.d"
    common_dir.mkdir(parents=True)

    (common_dir / "broken.conf").write_text("[[[[broken")

    with pytest.raises(ServiceDiscoveryError):
        discover_common_dropins(fujin_dir)


def test_discover_service_dropins(tmp_path):
    """Should discover service-specific dropins."""
    fujin_dir = tmp_path / ".fujin"
    systemd_dir = fujin_dir / "systemd"
    dropin_dir = systemd_dir / "web.service.d"
    dropin_dir.mkdir(parents=True)

    (dropin_dir / "resources.conf").write_text("""[Service]
MemoryMax=512M
""")

    dropins = discover_service_dropins(fujin_dir, "web", is_template=False)

    assert len(dropins) == 1
    assert dropins[0].name == "resources.conf"


def test_discover_service_dropins_template(tmp_path):
    """Should discover dropins for template service."""
    fujin_dir = tmp_path / ".fujin"
    systemd_dir = fujin_dir / "systemd"
    dropin_dir = systemd_dir / "web@.service.d"
    dropin_dir.mkdir(parents=True)

    (dropin_dir / "resources.conf").write_text("""[Service]
MemoryMax=512M
""")

    dropins = discover_service_dropins(fujin_dir, "web", is_template=True)

    assert len(dropins) == 1
    assert dropins[0].name == "resources.conf"


def test_discover_service_dropins_no_directory(tmp_path):
    """Should return empty list if dropin directory doesn't exist."""
    fujin_dir = tmp_path / ".fujin"
    fujin_dir.mkdir()

    dropins = discover_service_dropins(fujin_dir, "web", is_template=False)
    assert dropins == []
