"""Integration tests for full deployment workflows.

These tests use Docker containers to simulate a real VPS environment.
"""

from __future__ import annotations

import subprocess
import time
import zipfile
from pathlib import Path
from unittest.mock import patch

import msgspec

from fujin.commands.deploy import Deploy
from fujin.commands.down import Down
from fujin.commands.rollback import Rollback
from fujin.config import Config


def exec_in_container(container_name: str, cmd: str) -> tuple[str, bool]:
    """Execute command in container and return (stdout, success)."""
    result = subprocess.run(
        ["docker", "exec", container_name, "bash", "-c", cmd],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip(), result.returncode == 0


def create_minimal_wheel(wheel_path: Path, name: str, version: str):
    """Create a minimal valid wheel file for testing."""
    with zipfile.ZipFile(wheel_path, "w", zipfile.ZIP_DEFLATED) as whl:
        # Create minimal METADATA file
        metadata = f"""Metadata-Version: 2.1
Name: {name}
Version: {version}
"""
        whl.writestr(f"{name}-{version}.dist-info/METADATA", metadata)

        # Create WHEEL file
        wheel_info = """Wheel-Version: 1.0
Generator: test
Root-Is-Purelib: true
Tag: py3-none-any
"""
        whl.writestr(f"{name}-{version}.dist-info/WHEEL", wheel_info)

        # Create RECORD file (can be empty for tests)
        whl.writestr(f"{name}-{version}.dist-info/RECORD", "")


def test_binary_deployment(vps_container, ssh_key, tmp_path, monkeypatch):
    """Deploy a binary application end-to-end."""
    monkeypatch.chdir(tmp_path)

    # Create mock binary (simple HTTP server)
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    distfile = dist_dir / "myapp-1.0.0"

    script_content = """#!/usr/bin/env python3
import http.server
import socketserver

PORT = 8000
Handler = http.server.SimpleHTTPRequestHandler

with socketserver.TCPServer(("", PORT), Handler) as httpd:
    print(f"Server running on port {PORT}")
    httpd.serve_forever()
"""
    distfile.write_text(script_content)
    distfile.chmod(0o755)

    # Create config
    config_dict = {
        "app": "myapp",
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
        "processes": {"web": {"command": "myapp"}},  # Binary name matches app_bin
    }

    config = msgspec.convert(config_dict, type=Config)

    # Mock Config.read to return our config
    with patch("fujin.config.Config.read", return_value=config):
        # Deploy with no_input to suppress prompts
        deploy = Deploy(no_input=True)
        deploy()

    # Verify service is active
    stdout, success = exec_in_container(
        vps_container["name"], "systemctl is-active myapp.service"
    )

    if not success or stdout != "active":
        # Debug: show service status and logs
        status, _ = exec_in_container(
            vps_container["name"], "systemctl status myapp.service"
        )
        logs, _ = exec_in_container(
            vps_container["name"], "journalctl -u myapp.service --no-pager -n 50"
        )
        print(f"\n=== Service Status ===\n{status}")
        print(f"\n=== Service Logs ===\n{logs}")

    assert success, f"Service not active: {stdout}"
    assert stdout == "active"

    # Verify app is responding
    for _ in range(10):
        stdout, success = exec_in_container(
            vps_container["name"],
            "curl -s -o /dev/null -w '%{http_code}' http://localhost:8000",
        )
        if stdout == "200":
            break
        time.sleep(1)

    assert stdout == "200", f"App not responding, got: {stdout}"


def test_python_package_deployment(vps_container, ssh_key, tmp_path, monkeypatch):
    """Deploy a Python package application end-to-end."""
    monkeypatch.chdir(tmp_path)

    # Create minimal valid wheel
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    wheel_file = dist_dir / "testapp-2.0.0-py3-none-any.whl"
    create_minimal_wheel(wheel_file, "testapp", "2.0.0")

    # Create requirements.txt
    requirements_file = tmp_path / "requirements.txt"
    requirements_file.write_text("")

    # Create pyproject.toml
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "testapp"\nversion = "2.0.0"\n')

    # Create .env file
    env_file = tmp_path / ".env"
    env_file.write_text("DEBUG=true\n")

    # Create config
    config_dict = {
        "app": "testapp",
        "version": "2.0.0",
        "build_command": "echo 'building'",
        "distfile": "dist/testapp-{version}-py3-none-any.whl",
        "installation_mode": "python-package",
        "python_version": "3.11",
        "requirements": str(requirements_file),
        "hosts": [
            {
                "address": vps_container["ip"],
                "user": vps_container["user"],
                "port": vps_container["port"],
                "key_filename": ssh_key,
                "envfile": str(env_file),
            }
        ],
        "processes": {
            "web": {"command": ".venv/bin/python3 -m http.server 8000"},
            "worker": {
                "command": ".venv/bin/python3 -c 'import time; print(\"worker running\"); time.sleep(99999)'"
            },
        },
    }

    config = msgspec.convert(config_dict, type=Config)

    with patch("fujin.config.Config.read", return_value=config):
        # Deploy with no_input to suppress prompts
        deploy = Deploy(no_input=True)
        deploy()

    # Verify both services are active (with retry for slow starts)
    for service in ["testapp.service", "testapp-worker.service"]:
        for attempt in range(10):
            stdout, success = exec_in_container(
                vps_container["name"], f"systemctl is-active {service}"
            )
            if success and stdout == "active":
                break
            time.sleep(1)
        else:
            # Show debug info if service didn't start
            status, _ = exec_in_container(
                vps_container["name"], f"systemctl status {service}"
            )
            print(f"\n=== {service} Status ===\n{status}")
            assert False, f"{service} not active after 10s: {stdout}"

    # Verify .env was deployed
    stdout, success = exec_in_container(
        vps_container["name"],
        f"cat /home/{vps_container['user']}/.local/share/fujin/testapp/.env",
    )
    assert success and "DEBUG=true" in stdout, f".env not deployed correctly: {stdout}"


def test_deployment_with_webserver(vps_container, ssh_key, tmp_path, monkeypatch):
    """Deploy with Caddy webserver configuration."""
    monkeypatch.chdir(tmp_path)

    # Create mock binary
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    distfile = dist_dir / "webapp-1.0.0"
    distfile.write_text("#!/bin/bash\nsleep infinity\n")
    distfile.chmod(0o755)

    config_dict = {
        "app": "webapp",
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
        "processes": {"web": {"command": "webapp", "listen": "localhost:8000"}},
        "sites": [
            {
                "domains": ["example.com"],
                "routes": {"/static/*": {"static": "/var/www/static/"}, "/": "web"},
            }
        ],
    }

    config = msgspec.convert(config_dict, type=Config)

    with patch("fujin.config.Config.read", return_value=config):
        deploy = Deploy(no_input=True)
        deploy()

    # Verify Caddyfile was created
    stdout, success = exec_in_container(
        vps_container["name"], "cat /etc/caddy/conf.d/webapp.caddy"
    )
    assert success, "Caddyfile not created"
    assert "example.com" in stdout
    assert "localhost:8000" in stdout
    assert "/static/*" in stdout

    # Verify Caddy was reloaded
    stdout, success = exec_in_container(
        vps_container["name"], "systemctl is-active caddy"
    )
    assert success and stdout == "active"


def test_rollback_to_previous_version(vps_container, ssh_key, tmp_path, monkeypatch):
    """Deploy two versions and rollback to first."""
    monkeypatch.chdir(tmp_path)

    # Create pyproject.toml
    pyproject = tmp_path / "pyproject.toml"

    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()

    # Helper to create config for a version
    def make_config(version: str):
        pyproject.write_text(f'[project]\nname = "rollapp"\nversion = "{version}"\n')
        distfile = dist_dir / f"rollapp-{version}"
        distfile.write_text(
            f"#!/bin/bash\necho 'version {version}' && sleep infinity\n"
        )
        distfile.chmod(0o755)

        return msgspec.convert(
            {
                "app": "rollapp",
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
                "processes": {"web": {"command": "rollapp"}},
            },
            type=Config,
        )

    # Deploy v1.0.0
    config_v1 = make_config("1.0.0")
    with patch("fujin.config.Config.read", return_value=config_v1):
        deploy = Deploy(no_input=True)
        deploy()

    # Verify v1.0.0 is running
    stdout, success = exec_in_container(
        vps_container["name"],
        f"cat /home/{vps_container['user']}/.local/share/fujin/rollapp/.version",
    )
    assert success and stdout == "1.0.0", f"Expected version 1.0.0, got: '{stdout}'"

    # Deploy v2.0.0
    config_v2 = make_config("2.0.0")
    with patch("fujin.config.Config.read", return_value=config_v2):
        deploy = Deploy(no_input=True)
        deploy()

    # Verify v2.0.0 is running
    stdout, success = exec_in_container(
        vps_container["name"],
        f"cat /home/{vps_container['user']}/.local/share/fujin/rollapp/.version",
    )
    assert success and stdout == "2.0.0", f"Expected version 2.0.0, got: '{stdout}'"

    # Rollback to v1.0.0
    with (
        patch("fujin.config.Config.read", return_value=config_v2),
        patch("fujin.commands.rollback.Prompt.ask", return_value="1.0.0"),
        patch("fujin.commands.rollback.Confirm.ask", return_value=True),
    ):
        rollback = Rollback()
        rollback()

    # Verify v1.0.0 is running again
    stdout, success = exec_in_container(
        vps_container["name"],
        f"cat /home/{vps_container['user']}/.local/share/fujin/rollapp/.version",
    )
    assert success and stdout == "1.0.0", (
        f"Expected version 1.0.0 after rollback, got: '{stdout}'"
    )

    # Verify service is still active
    stdout, success = exec_in_container(
        vps_container["name"], "systemctl is-active rollapp.service"
    )
    assert success and stdout == "active"


def test_down_command(vps_container, ssh_key, tmp_path, monkeypatch):
    """Deploy and then stop services with down command."""
    monkeypatch.chdir(tmp_path)

    # Create mock binary
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    distfile = dist_dir / "downapp-1.0.0"
    distfile.write_text("#!/bin/bash\nsleep infinity\n")
    distfile.chmod(0o755)

    # Create pyproject.toml
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "downapp"\nversion = "1.0.0"\n')

    config_dict = {
        "app": "downapp",
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
        "processes": {"web": {"command": "downapp"}},
    }

    config = msgspec.convert(config_dict, type=Config)

    # Deploy
    with patch("fujin.config.Config.read", return_value=config):
        deploy = Deploy(no_input=True)
        deploy()

    # Verify service is active
    stdout, success = exec_in_container(
        vps_container["name"], "systemctl is-active downapp.service"
    )
    assert success and stdout == "active"

    # Run down command
    with (
        patch("fujin.config.Config.read", return_value=config),
        patch("fujin.commands.down.Confirm.ask", return_value=True),
    ):
        down = Down()
        down()

    # Verify service is inactive or doesn't exist
    stdout, _ = exec_in_container(
        vps_container["name"], "systemctl is-active downapp.service"
    )
    assert stdout in ["inactive", "unknown"], (
        f"Expected inactive or unknown, got: {stdout}"
    )

    # Verify service files were removed (down runs uninstall)
    _, success = exec_in_container(
        vps_container["name"], "test -f /etc/systemd/system/downapp.service"
    )
    assert not success, "Service file should be removed after down"
