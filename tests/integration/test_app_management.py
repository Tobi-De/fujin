"""Integration tests for app management commands.

These tests verify that app restart, logs, status, and cat commands work correctly.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import msgspec
import pytest

from fujin.commands.app import App
from fujin.commands.deploy import Deploy
from fujin.config import Config

from .helpers import (
    assert_service_running,
    exec_in_container,
    wait_for_service,
)


@pytest.fixture(scope="function")
def deployed_app(vps_container, ssh_key, tmp_path):
    """Deploy app for test.

    Creates an app with two services (web + worker) that tests can use.
    Returns tuple of (config, deploy_path) so tests can chdir to the right location.
    """
    # Create binary distfile
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir(exist_ok=True)
    distfile = dist_dir / "mgmtapp-1.0.0"
    distfile.write_text("#!/bin/bash\nwhile true; do echo 'running'; sleep 5; done\n")
    distfile.chmod(0o755)

    # Create systemd unit files
    install_dir = tmp_path / ".fujin"
    systemd_dir = install_dir / "systemd"
    systemd_dir.mkdir(parents=True, exist_ok=True)

    # Web service
    (systemd_dir / "web.service").write_text("""[Unit]
Description=Web server

[Service]
Type=simple
ExecStart={install_dir}/mgmtapp
WorkingDirectory={app_dir}
Restart=always

[Install]
WantedBy=multi-user.target
""")

    # Worker service
    (systemd_dir / "worker.service").write_text("""[Unit]
Description=Worker

[Service]
Type=simple
ExecStart={install_dir}/mgmtapp
WorkingDirectory={app_dir}
Restart=always

[Install]
WantedBy=multi-user.target
""")

    # Create env file
    (install_dir / "env").write_text("DEBUG=true\nAPP_NAME=mgmtapp\n")

    config_dict = {
        "app": "mgmtapp",
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

    # Change to tmp_path for deploy
    old_cwd = os.getcwd()
    os.chdir(tmp_path)

    try:
        with patch("fujin.config.Config.read", return_value=config):
            deploy = Deploy(no_input=True)
            deploy()

        wait_for_service(vps_container["name"], "mgmtapp-web.service")
        wait_for_service(vps_container["name"], "mgmtapp-worker.service")
    finally:
        os.chdir(old_cwd)

    return (config, tmp_path)


def test_service_lifecycle(deployed_app, vps_container, monkeypatch):
    """Test restart single service, stop/start, and restart all services.

    This test combines three lifecycle operations:
    1. Restart single service (web) - verifies PID changes
    2. Stop and start service - verifies state transitions
    3. Restart all services - verifies both services restart
    """
    config, deploy_path = deployed_app
    monkeypatch.chdir(deploy_path)

    # Part 1: Test single service restart
    stdout, _ = exec_in_container(
        vps_container["name"],
        "systemctl show mgmtapp-web.service --property=MainPID --value",
    )
    initial_pid = stdout.strip()

    with patch("fujin.config.Config.read", return_value=config):
        app = App()
        app.restart(name="web")

    wait_for_service(vps_container["name"], "mgmtapp-web.service")

    stdout, _ = exec_in_container(
        vps_container["name"],
        "systemctl show mgmtapp-web.service --property=MainPID --value",
    )
    new_pid = stdout.strip()
    assert new_pid != initial_pid, f"PID should change after restart"
    assert new_pid != "0", "Service should have valid PID"

    # Part 2: Test stop and start
    assert_service_running(vps_container["name"], "mgmtapp-web.service")

    with patch("fujin.config.Config.read", return_value=config):
        app = App()
        app.stop(name="web")

    stdout, _ = exec_in_container(
        vps_container["name"], "systemctl is-active mgmtapp-web.service"
    )
    assert stdout in ["inactive", "failed"], f"Service should be stopped: {stdout}"

    with patch("fujin.config.Config.read", return_value=config):
        app = App()
        app.start(name="web")

    wait_for_service(vps_container["name"], "mgmtapp-web.service")

    # Part 3: Test restart all (both web and worker)
    web_pid_before, _ = exec_in_container(
        vps_container["name"],
        "systemctl show mgmtapp-web.service --property=MainPID --value",
    )
    worker_pid_before, _ = exec_in_container(
        vps_container["name"],
        "systemctl show mgmtapp-worker.service --property=MainPID --value",
    )

    with patch("fujin.config.Config.read", return_value=config):
        app = App()
        app.restart(name=None)  # Restart all

    wait_for_service(vps_container["name"], "mgmtapp-web.service")
    wait_for_service(vps_container["name"], "mgmtapp-worker.service")

    web_pid_after, _ = exec_in_container(
        vps_container["name"],
        "systemctl show mgmtapp-web.service --property=MainPID --value",
    )
    worker_pid_after, _ = exec_in_container(
        vps_container["name"],
        "systemctl show mgmtapp-worker.service --property=MainPID --value",
    )

    assert web_pid_before.strip() != web_pid_after.strip(), "Web PID should change"
    assert worker_pid_before.strip() != worker_pid_after.strip(), (
        "Worker PID should change"
    )


def test_app_introspection(deployed_app, vps_container, monkeypatch):
    """Test logs, cat unit, and cat env commands.

    This test combines three introspection operations:
    1. View service logs via journalctl
    2. View systemd unit file content
    3. View environment file content
    """
    config, deploy_path = deployed_app
    monkeypatch.chdir(deploy_path)

    # Part 1: Test logs
    import time

    time.sleep(0.5)  # Brief wait for logs to accumulate

    with patch("fujin.config.Config.read", return_value=config):
        app = App()
        app.logs(name="web", lines=10, follow=False)

    stdout, success = exec_in_container(
        vps_container["name"],
        "journalctl -u mgmtapp-web.service -n 5 --no-pager",
    )
    assert success, f"journalctl should succeed: {stdout}"

    # Part 2: Test cat unit file
    with patch("fujin.config.Config.read", return_value=config):
        app = App()
        app.cat(name="web")

    stdout, success = exec_in_container(
        vps_container["name"],
        "systemctl cat mgmtapp-web.service --no-pager",
    )
    assert success, f"systemctl cat should succeed: {stdout}"
    assert "mgmtapp" in stdout, f"Unit file should reference mgmtapp: {stdout}"

    # Part 3: Test cat env file
    with patch("fujin.config.Config.read", return_value=config):
        app = App()
        app.cat(name="env")

    stdout, success = exec_in_container(
        vps_container["name"],
        "cat /opt/fujin/mgmtapp/.install/.env",
    )
    assert success, f"Reading .env should succeed: {stdout}"
    # Note: .env file exists but may be empty for binary installations
    # The important thing is that the cat command works
