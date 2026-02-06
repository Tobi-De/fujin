"""Tests for service discovery."""

from __future__ import annotations

import pytest

from fujin.discovery import ServiceDiscoveryError, discover_deployed_units


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
    assert units[0].name == "web"
    assert units[0].is_template is False
    assert units[0].service_file == service_file
    assert units[0].socket_file is None
    assert units[0].timer_file is None
    assert units[0].template_service_name == "myapp-web.service"
    assert units[0].replicas == 1
    assert units[0].service_instances() == ["myapp-web.service"]


def test_discover_template_service(tmp_path):
    """Should discover a template service (with @) when replicas > 1."""
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
    assert units[0].name == "web"
    assert units[0].is_template is True
    assert units[0].service_file == service_file
    assert units[0].template_service_name == "myapp-web@.service"
    assert units[0].replicas == 3
    assert units[0].service_instances() == [
        "myapp-web@1.service",
        "myapp-web@2.service",
        "myapp-web@3.service",
    ]


def test_discover_service_with_socket(tmp_path):
    """Should discover service with socket (sockets are always singletons)."""
    install_dir = tmp_path / ".fujin"
    systemd_dir = install_dir / "systemd"
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

    units = discover_deployed_units(install_dir, "myapp", {})
    assert len(units) == 1
    assert units[0].socket_file == socket_file
    assert units[0].template_socket_name == "myapp-web.socket"


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
    names = [u.name for u in units]
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


def test_is_template_derived_from_replicas(tmp_path):
    """is_template should be derived from replicas count, not file naming."""
    install_dir = tmp_path / ".fujin"
    systemd_dir = install_dir / "systemd"
    systemd_dir.mkdir(parents=True)

    # Create a template service file
    service_file = systemd_dir / "worker@.service"
    service_file.write_text("""[Unit]
Description=Worker %i

[Service]
ExecStart=/bin/true

[Install]
WantedBy=multi-user.target
""")

    # Without replicas config, defaults to 1
    units = discover_deployed_units(install_dir, "myapp", {})
    assert len(units) == 1
    assert units[0].replicas == 1
    # is_template is derived from replicas > 1
    assert units[0].is_template is False

    # With replicas > 1
    units = discover_deployed_units(install_dir, "myapp", {"worker": 2})
    assert len(units) == 1
    assert units[0].replicas == 2
    assert units[0].is_template is True


def test_all_runtime_units(tmp_path):
    """all_runtime_units should include service instances and auxiliary units."""
    install_dir = tmp_path / ".fujin"
    systemd_dir = install_dir / "systemd"
    systemd_dir.mkdir(parents=True)

    service_file = systemd_dir / "web.service"
    service_file.write_text("""[Unit]
Description=Web

[Service]
ExecStart=/bin/true
""")

    socket_file = systemd_dir / "web.socket"
    socket_file.write_text("""[Unit]
Description=Socket

[Socket]
ListenStream=/run/web.sock
""")

    units = discover_deployed_units(install_dir, "myapp", {})
    assert len(units) == 1
    assert units[0].all_runtime_units() == [
        "myapp-web.service",
        "myapp-web.socket",
    ]


def test_discover_skips_underscore_prefixed_services(tmp_path):
    """Should skip services with names starting with underscore."""
    install_dir = tmp_path / ".fujin"
    systemd_dir = install_dir / "systemd"
    systemd_dir.mkdir(parents=True)

    # Create a regular service
    (systemd_dir / "web.service").write_text("""[Unit]
Description=Web server

[Service]
ExecStart=/bin/true
""")

    # Create an underscore-prefixed service (should be skipped)
    (systemd_dir / "_helper.service").write_text("""[Unit]
Description=Internal helper

[Service]
ExecStart=/bin/true
""")

    units = discover_deployed_units(install_dir, "myapp", {})

    assert len(units) == 1
    assert units[0].name == "web"


def test_discover_skips_underscore_template_services(tmp_path):
    """Should skip template services with names starting with underscore."""
    install_dir = tmp_path / ".fujin"
    systemd_dir = install_dir / "systemd"
    systemd_dir.mkdir(parents=True)

    # Create a regular template service
    (systemd_dir / "worker@.service").write_text("""[Unit]
Description=Worker %i

[Service]
ExecStart=/bin/true
""")

    # Create an underscore-prefixed template service (should be skipped)
    (systemd_dir / "_internal@.service").write_text("""[Unit]
Description=Internal %i

[Service]
ExecStart=/bin/true
""")

    units = discover_deployed_units(install_dir, "myapp", {"worker": 2, "_internal": 2})

    assert len(units) == 1
    assert units[0].name == "worker"


def test_discover_skips_all_underscore_services(tmp_path):
    """Should return empty list if all services are underscore-prefixed."""
    install_dir = tmp_path / ".fujin"
    systemd_dir = install_dir / "systemd"
    systemd_dir.mkdir(parents=True)

    # Create only underscore-prefixed services
    (systemd_dir / "_helper.service").write_text("""[Unit]
Description=Helper

[Service]
ExecStart=/bin/true
""")

    (systemd_dir / "_internal.service").write_text("""[Unit]
Description=Internal

[Service]
ExecStart=/bin/true
""")

    units = discover_deployed_units(install_dir, "myapp", {})

    assert units == []


def test_discover_underscore_files_not_validated(tmp_path):
    """Underscore-prefixed files should not be validated (can be malformed)."""
    install_dir = tmp_path / ".fujin"
    systemd_dir = install_dir / "systemd"
    systemd_dir.mkdir(parents=True)

    # Create a valid regular service
    (systemd_dir / "web.service").write_text("""[Unit]
Description=Web

[Service]
ExecStart=/bin/true
""")

    # Create a malformed underscore-prefixed service (should be skipped before validation)
    (systemd_dir / "_broken.service").write_text("This is not valid INI\n[[[broken")

    # Should not raise an error because _broken.service is skipped before validation
    units = discover_deployed_units(install_dir, "myapp", {})

    assert len(units) == 1
    assert units[0].name == "web"
