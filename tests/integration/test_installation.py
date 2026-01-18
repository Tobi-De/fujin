"""Integration tests for installation scenarios.

These tests cover installation modes, systemd units (sockets, timers),
stale unit cleanup, and dropin directory handling.
"""

from __future__ import annotations

from unittest.mock import patch

import msgspec

from fujin.commands.deploy import Deploy
from fujin.config import Config

from .helpers import (
    assert_dir_exists,
    assert_dir_not_exists,
    assert_file_contains,
    assert_file_exists,
    assert_file_not_exists,
    assert_systemd_unit_exists,
    assert_systemd_unit_not_exists,
    exec_in_container,
    wait_for_service,
)


def test_socket_activated_service(vps_container, ssh_key, tmp_path, monkeypatch):
    """Deploy a socket-activated service and verify it's enabled."""
    monkeypatch.chdir(tmp_path)

    # Create mock binary
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    distfile = dist_dir / "socketapp-1.0.0"
    distfile.write_text("#!/bin/bash\nsleep infinity\n")
    distfile.chmod(0o755)

    # Create .fujin/systemd directory with service and socket files
    install_dir = tmp_path / ".fujin"
    systemd_dir = install_dir / "systemd"
    systemd_dir.mkdir(parents=True)

    # Web service that accepts socket activation
    service_file = systemd_dir / "web.service"
    service_file.write_text("""[Unit]
Description=Socket-activated web server
Requires={app_name}-web.socket
After={app_name}-web.socket

[Service]
Type=simple
ExecStart={install_dir}/socketapp
WorkingDirectory={app_dir}
StandardInput=socket

[Install]
WantedBy=multi-user.target
""")

    # Socket unit
    socket_file = systemd_dir / "web.socket"
    socket_file.write_text("""[Unit]
Description=Socket for web server

[Socket]
ListenStream=8080
Accept=no

[Install]
WantedBy=sockets.target
""")

    config_dict = {
        "app": "socketapp",
        "version": "1.0.0",
        "build_command": "echo 'building'",
        "distfile": str(distfile),
        "installation_mode": "binary",
        "hosts": [
            {
                "address": vps_container["ip"],
                "user": vps_container["user"],
                "port": vps_container["port"],
                "key_filename": ssh_key,
            }
        ],
    }

    config = msgspec.convert(config_dict, type=Config)

    with patch("fujin.config.Config.read", return_value=config):
        deploy = Deploy(no_input=True)
        deploy()

    # Verify socket unit was installed
    assert_systemd_unit_exists(vps_container["name"], "socketapp-web.socket")
    assert_systemd_unit_exists(vps_container["name"], "socketapp-web.service")

    # Verify socket is listening
    stdout, success = exec_in_container(
        vps_container["name"], "systemctl is-active socketapp-web.socket"
    )
    assert success and stdout == "active", f"Socket not active: {stdout}"

    # Service should also be active (since we start it with the socket)
    wait_for_service(vps_container["name"], "socketapp-web.service")


def test_timer_scheduled_service(vps_container, ssh_key, tmp_path, monkeypatch):
    """Deploy a timer-scheduled service and verify it's enabled."""
    monkeypatch.chdir(tmp_path)

    # Create mock binary
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    distfile = dist_dir / "timerapp-1.0.0"
    distfile.write_text("#!/bin/bash\necho 'task running' && exit 0\n")
    distfile.chmod(0o755)

    # Create .fujin/systemd directory with service and timer files
    install_dir = tmp_path / ".fujin"
    systemd_dir = install_dir / "systemd"
    systemd_dir.mkdir(parents=True)

    # Oneshot service triggered by timer
    service_file = systemd_dir / "cleanup.service"
    service_file.write_text("""[Unit]
Description=Cleanup task

[Service]
Type=oneshot
ExecStart={install_dir}/timerapp
WorkingDirectory={app_dir}
""")

    # Timer unit
    timer_file = systemd_dir / "cleanup.timer"
    timer_file.write_text("""[Unit]
Description=Run cleanup daily

[Timer]
OnCalendar=daily
Persistent=true

[Install]
WantedBy=timers.target
""")

    config_dict = {
        "app": "timerapp",
        "version": "1.0.0",
        "build_command": "echo 'building'",
        "distfile": str(distfile),
        "installation_mode": "binary",
        "hosts": [
            {
                "address": vps_container["ip"],
                "user": vps_container["user"],
                "port": vps_container["port"],
                "key_filename": ssh_key,
            }
        ],
    }

    config = msgspec.convert(config_dict, type=Config)

    with patch("fujin.config.Config.read", return_value=config):
        deploy = Deploy(no_input=True)
        deploy()

    # Verify timer unit was installed
    assert_systemd_unit_exists(vps_container["name"], "timerapp-cleanup.timer")
    assert_systemd_unit_exists(vps_container["name"], "timerapp-cleanup.service")

    # Verify timer is active
    stdout, success = exec_in_container(
        vps_container["name"], "systemctl is-active timerapp-cleanup.timer"
    )
    assert success and stdout == "active", f"Timer not active: {stdout}"

    # Service won't be active (it's oneshot, triggered by timer)
    # But the timer should be waiting
    stdout, _ = exec_in_container(
        vps_container["name"], "systemctl list-timers timerapp-cleanup.timer --no-pager"
    )
    assert "timerapp-cleanup.timer" in stdout


def test_stale_unit_cleanup(vps_container, ssh_key, tmp_path, monkeypatch):
    """Deploy an app, then deploy a version that removes a service."""
    monkeypatch.chdir(tmp_path)

    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()

    # Create .fujin/systemd directory
    install_dir = tmp_path / ".fujin"
    systemd_dir = install_dir / "systemd"
    systemd_dir.mkdir(parents=True)

    def create_config(version: str):
        distfile = dist_dir / f"staleapp-{version}"
        distfile.write_text("#!/bin/bash\nsleep infinity\n")
        distfile.chmod(0o755)
        return msgspec.convert(
            {
                "app": "staleapp",
                "version": version,
                "build_command": "echo 'building'",
                "distfile": str(distfile),
                "installation_mode": "binary",
                "hosts": [
                    {
                        "address": vps_container["ip"],
                        "user": vps_container["user"],
                        "port": vps_container["port"],
                        "key_filename": ssh_key,
                    }
                ],
            },
            type=Config,
        )

    # Deploy v1 with web and worker services
    web_service = systemd_dir / "web.service"
    web_service.write_text("""[Unit]
Description=Web server

[Service]
Type=simple
ExecStart={install_dir}/staleapp
WorkingDirectory={app_dir}
Restart=always

[Install]
WantedBy=multi-user.target
""")

    worker_service = systemd_dir / "worker.service"
    worker_service.write_text("""[Unit]
Description=Worker

[Service]
Type=simple
ExecStart={install_dir}/staleapp
WorkingDirectory={app_dir}
Restart=always

[Install]
WantedBy=multi-user.target
""")

    config_v1 = create_config("1.0.0")
    with patch("fujin.config.Config.read", return_value=config_v1):
        deploy = Deploy(no_input=True)
        deploy()

    # Verify both services are running
    wait_for_service(vps_container["name"], "staleapp-web.service")
    wait_for_service(vps_container["name"], "staleapp-worker.service")
    assert_systemd_unit_exists(vps_container["name"], "staleapp-worker.service")

    # Deploy v2 with only web service (worker removed)
    worker_service.unlink()

    config_v2 = create_config("2.0.0")
    with patch("fujin.config.Config.read", return_value=config_v2):
        deploy = Deploy(no_input=True)
        deploy()

    # Verify web is still running
    wait_for_service(vps_container["name"], "staleapp-web.service")

    # Verify worker service was removed (stale unit cleanup)
    stdout, _ = exec_in_container(
        vps_container["name"], "systemctl is-active staleapp-worker.service"
    )
    assert stdout in ["inactive", "unknown"], f"Worker should be stopped: {stdout}"

    # Unit file should be removed
    assert_systemd_unit_not_exists(vps_container["name"], "staleapp-worker.service")


def test_dropin_directory_handling(vps_container, ssh_key, tmp_path, monkeypatch):
    """Deploy with dropin directories and verify they're applied."""
    monkeypatch.chdir(tmp_path)

    # Create mock binary
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    distfile = dist_dir / "dropinapp-1.0.0"
    distfile.write_text("#!/bin/bash\nsleep infinity\n")
    distfile.chmod(0o755)

    # Create .fujin/systemd directory
    install_dir = tmp_path / ".fujin"
    systemd_dir = install_dir / "systemd"
    systemd_dir.mkdir(parents=True)

    # Web service
    web_service = systemd_dir / "web.service"
    web_service.write_text("""[Unit]
Description=Web server

[Service]
Type=simple
ExecStart={install_dir}/dropinapp
WorkingDirectory={app_dir}
Restart=always

[Install]
WantedBy=multi-user.target
""")

    # Common dropin (applies to all services)
    common_dir = systemd_dir / "common.d"
    common_dir.mkdir()
    common_dropin = common_dir / "limits.conf"
    common_dropin.write_text("""[Service]
LimitNOFILE=65536
""")

    # Service-specific dropin
    web_dropin_dir = systemd_dir / "web.service.d"
    web_dropin_dir.mkdir()
    web_dropin = web_dropin_dir / "memory.conf"
    web_dropin.write_text("""[Service]
MemoryMax=512M
""")

    config_dict = {
        "app": "dropinapp",
        "version": "1.0.0",
        "build_command": "echo 'building'",
        "distfile": str(distfile),
        "installation_mode": "binary",
        "hosts": [
            {
                "address": vps_container["ip"],
                "user": vps_container["user"],
                "port": vps_container["port"],
                "key_filename": ssh_key,
            }
        ],
    }

    config = msgspec.convert(config_dict, type=Config)

    with patch("fujin.config.Config.read", return_value=config):
        deploy = Deploy(no_input=True)
        deploy()

    # Verify service is running
    wait_for_service(vps_container["name"], "dropinapp-web.service")

    # Verify dropin directory was created
    assert_dir_exists(
        vps_container["name"], "/etc/systemd/system/dropinapp-web.service.d"
    )

    # Verify common dropin was applied
    assert_file_exists(
        vps_container["name"],
        "/etc/systemd/system/dropinapp-web.service.d/limits.conf",
    )
    assert_file_contains(
        vps_container["name"],
        "/etc/systemd/system/dropinapp-web.service.d/limits.conf",
        "LimitNOFILE=65536",
    )

    # Verify service-specific dropin was applied
    assert_file_exists(
        vps_container["name"],
        "/etc/systemd/system/dropinapp-web.service.d/memory.conf",
    )
    assert_file_contains(
        vps_container["name"],
        "/etc/systemd/system/dropinapp-web.service.d/memory.conf",
        "MemoryMax=512M",
    )

    # Verify the dropin is actually being used by systemd
    stdout, success = exec_in_container(
        vps_container["name"], "systemctl show dropinapp-web.service -p LimitNOFILE"
    )
    assert success
    assert "65536" in stdout, f"LimitNOFILE not applied: {stdout}"


def test_stale_dropin_cleanup(vps_container, ssh_key, tmp_path, monkeypatch):
    """Deploy with dropins, then deploy without them and verify cleanup."""
    monkeypatch.chdir(tmp_path)

    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()

    install_dir = tmp_path / ".fujin"
    systemd_dir = install_dir / "systemd"
    systemd_dir.mkdir(parents=True)

    def create_config(version: str):
        distfile = dist_dir / f"dropinclean-{version}"
        distfile.write_text("#!/bin/bash\nsleep infinity\n")
        distfile.chmod(0o755)
        return msgspec.convert(
            {
                "app": "dropinclean",
                "version": version,
                "build_command": "echo 'building'",
                "distfile": str(distfile),
                "installation_mode": "binary",
                "hosts": [
                    {
                        "address": vps_container["ip"],
                        "user": vps_container["user"],
                        "port": vps_container["port"],
                        "key_filename": ssh_key,
                    }
                ],
            },
            type=Config,
        )

    # Web service
    web_service = systemd_dir / "web.service"
    web_service.write_text("""[Unit]
Description=Web server

[Service]
Type=simple
ExecStart={install_dir}/dropinclean
WorkingDirectory={app_dir}
Restart=always

[Install]
WantedBy=multi-user.target
""")

    # Deploy v1 with dropin
    common_dir = systemd_dir / "common.d"
    common_dir.mkdir()
    old_dropin = common_dir / "old-config.conf"
    old_dropin.write_text("""[Service]
Nice=10
""")

    config_v1 = create_config("1.0.0")
    with patch("fujin.config.Config.read", return_value=config_v1):
        deploy = Deploy(no_input=True)
        deploy()

    # Verify dropin exists
    assert_file_exists(
        vps_container["name"],
        "/etc/systemd/system/dropinclean-web.service.d/old-config.conf",
    )

    # Deploy v2 without dropin (remove the dropin file)
    old_dropin.unlink()
    common_dir.rmdir()

    config_v2 = create_config("2.0.0")
    with patch("fujin.config.Config.read", return_value=config_v2):
        deploy = Deploy(no_input=True)
        deploy()

    # Verify stale dropin was removed
    assert_file_not_exists(
        vps_container["name"],
        "/etc/systemd/system/dropinclean-web.service.d/old-config.conf",
    )

    # Verify empty dropin directory was removed
    assert_dir_not_exists(
        vps_container["name"], "/etc/systemd/system/dropinclean-web.service.d"
    )
