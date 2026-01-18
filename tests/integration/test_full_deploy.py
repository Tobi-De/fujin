"""Integration tests for full deployment workflows.

These tests use Docker containers to simulate a real VPS environment.
"""

from __future__ import annotations

import time
import zipfile
from pathlib import Path
from unittest.mock import patch

import msgspec

from fujin.commands.deploy import Deploy
from fujin.commands.down import Down
from fujin.commands.rollback import Rollback
from fujin.config import Config

from .helpers import exec_in_container, wait_for_service


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

    # Create .fujin/systemd directory with service file
    install_dir = tmp_path / ".fujin"
    systemd_dir = install_dir / "systemd"
    systemd_dir.mkdir(parents=True)

    service_file = systemd_dir / "web.service"
    service_file.write_text("""[Unit]
Description=Web server

[Service]
Type=simple
ExecStart={install_dir}/myapp
WorkingDirectory={app_dir}
EnvironmentFile=-{install_dir}/.env
Restart=always

[Install]
WantedBy=multi-user.target
""")

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
    }

    config = msgspec.convert(config_dict, type=Config)

    # Mock Config.read to return our config
    with patch("fujin.config.Config.read", return_value=config):
        # Deploy with no_input to suppress prompts
        deploy = Deploy(no_input=True)
        deploy()

    # Verify service is active
    stdout, success = exec_in_container(
        vps_container["name"], "systemctl is-active myapp-web.service"
    )

    if not success or stdout != "active":
        # Debug: show service status and logs
        status, _ = exec_in_container(
            vps_container["name"], "systemctl status myapp-web.service"
        )
        logs, _ = exec_in_container(
            vps_container["name"], "journalctl -u myapp-web.service --no-pager -n 50"
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

    # Create .fujin/systemd directory with service files
    install_dir = tmp_path / ".fujin"
    systemd_dir = install_dir / "systemd"
    systemd_dir.mkdir(parents=True)

    web_service = systemd_dir / "web.service"
    web_service.write_text("""[Unit]
Description=Web server

[Service]
Type=simple
ExecStart={install_dir}/.venv/bin/python3 -m http.server 8000
WorkingDirectory={app_dir}
EnvironmentFile=-{install_dir}/.env
Restart=always

[Install]
WantedBy=multi-user.target
""")

    worker_service = systemd_dir / "worker.service"
    worker_service.write_text("""[Unit]
Description=Worker

[Service]
Type=simple
ExecStart={install_dir}/.venv/bin/python3 -c 'import time; print("worker running"); time.sleep(99999)'
WorkingDirectory={app_dir}
EnvironmentFile=-{install_dir}/.env
Restart=always

[Install]
WantedBy=multi-user.target
""")

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
    }

    config = msgspec.convert(config_dict, type=Config)

    with patch("fujin.config.Config.read", return_value=config):
        # Deploy with no_input to suppress prompts
        deploy = Deploy(no_input=True)
        deploy()

    # Verify both services are active (with retry for slow starts)
    for service in ["testapp-web.service", "testapp-worker.service"]:
        wait_for_service(vps_container["name"], service)

    # Verify .env was deployed
    stdout, success = exec_in_container(
        vps_container["name"],
        "cat /opt/fujin/testapp/.install/.env",
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

    # Create .fujin/systemd directory with service file
    install_dir = tmp_path / ".fujin"
    systemd_dir = install_dir / "systemd"
    systemd_dir.mkdir(parents=True)

    service_file = systemd_dir / "web.service"
    service_file.write_text("""[Unit]
Description=Web server

[Service]
Type=simple
ExecStart={install_dir}/webapp
WorkingDirectory={app_dir}
EnvironmentFile=-{install_dir}/.env
Restart=always

[Install]
WantedBy=multi-user.target
""")

    # Create Caddyfile
    caddyfile = install_dir / "Caddyfile"
    caddyfile.write_text("""example.com {
    handle /static/* {
        root * /var/www/static/
        file_server
    }
    
    handle {
        reverse_proxy localhost:8000
    }
}
""")

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

    # Create .fujin/systemd directory with service file
    install_dir = tmp_path / ".fujin"
    systemd_dir = install_dir / "systemd"
    systemd_dir.mkdir(parents=True)

    service_file = systemd_dir / "web.service"
    service_file.write_text("""[Unit]
Description=Web server

[Service]
Type=simple
ExecStart={install_dir}/rollapp
WorkingDirectory={app_dir}
EnvironmentFile=-{install_dir}/.env
Restart=always

[Install]
WantedBy=multi-user.target
""")

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
        "cat /opt/fujin/rollapp/.install/.version",
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
        "cat /opt/fujin/rollapp/.install/.version",
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
        "cat /opt/fujin/rollapp/.install/.version",
    )
    assert success and stdout == "1.0.0", (
        f"Expected version 1.0.0 after rollback, got: '{stdout}'"
    )

    # Wait for service to be active after rollback
    wait_for_service(vps_container["name"], "rollapp-web.service")

    # Verify service is still active
    stdout, success = exec_in_container(
        vps_container["name"], "systemctl is-active rollapp-web.service"
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

    # Create .fujin/systemd directory with service file
    install_dir = tmp_path / ".fujin"
    systemd_dir = install_dir / "systemd"
    systemd_dir.mkdir(parents=True)

    service_file = systemd_dir / "web.service"
    service_file.write_text("""[Unit]
Description=Web server

[Service]
Type=simple
ExecStart={install_dir}/downapp
WorkingDirectory={app_dir}
EnvironmentFile=-{install_dir}/.env
Restart=always

[Install]
WantedBy=multi-user.target
""")

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
    }

    config = msgspec.convert(config_dict, type=Config)

    # Deploy
    with patch("fujin.config.Config.read", return_value=config):
        deploy = Deploy(no_input=True)
        deploy()

    # Verify service is active
    stdout, success = exec_in_container(
        vps_container["name"], "systemctl is-active downapp-web.service"
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
        vps_container["name"], "systemctl is-active downapp-web.service"
    )
    assert stdout in ["inactive", "unknown"], (
        f"Expected inactive or unknown, got: {stdout}"
    )

    # Verify service files were removed (down runs uninstall)
    _, success = exec_in_container(
        vps_container["name"], "test -f /etc/systemd/system/downapp-web.service"
    )
    assert not success, "Service file should be removed after down"

    # Verify app user was deleted
    _, success = exec_in_container(vps_container["name"], "id -u downapp")
    assert not success, "App user should be deleted after down"


def test_deploy_with_environment_secrets(vps_container, ssh_key, tmp_path, monkeypatch):
    """Deploy resolves environment variables and writes them to .env on server."""
    monkeypatch.chdir(tmp_path)

    # Set environment variables that will be resolved as secrets
    monkeypatch.setenv("MY_SECRET_KEY", "super-secret-value-123")
    monkeypatch.setenv("DATABASE_URL", "postgres://user:pass@localhost/db")

    # Create mock binary
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    distfile = dist_dir / "secretapp-1.0.0"
    distfile.write_text("#!/bin/bash\nsleep infinity\n")
    distfile.chmod(0o755)

    # Create .fujin directory with service and env file
    install_dir = tmp_path / ".fujin"
    systemd_dir = install_dir / "systemd"
    systemd_dir.mkdir(parents=True)

    service_file = systemd_dir / "web.service"
    service_file.write_text("""[Unit]
Description=Secret app

[Service]
Type=simple
ExecStart={install_dir}/secretapp
WorkingDirectory={app_dir}
EnvironmentFile={install_dir}/.env
Restart=always

[Install]
WantedBy=multi-user.target
""")

    # Create env file with secret references
    env_file = install_dir / "env"
    env_file.write_text("""DEBUG=false
SECRET_KEY=$MY_SECRET_KEY
DATABASE_URL=$DATABASE_URL
STATIC_VALUE=no-secret-here
""")

    config_dict = {
        "app": "secretapp",
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
                "envfile": ".fujin/env",
            }
        ],
        "secrets": {"adapter": "system"},
    }

    config = msgspec.convert(config_dict, type=Config)

    with patch("fujin.config.Config.read", return_value=config):
        deploy = Deploy(no_input=True)
        deploy()

    # Verify service is running
    wait_for_service(vps_container["name"], "secretapp-web.service")

    # Verify .env file was deployed with resolved secrets
    stdout, success = exec_in_container(
        vps_container["name"], "cat /opt/fujin/secretapp/.install/.env"
    )
    assert success, f"Could not read .env file: {stdout}"

    # Check that secrets were resolved (not literal $VAR references)
    assert "super-secret-value-123" in stdout, f"SECRET_KEY not resolved: {stdout}"
    assert "postgres://user:pass@localhost/db" in stdout, (
        f"DATABASE_URL not resolved: {stdout}"
    )
    assert "no-secret-here" in stdout, f"STATIC_VALUE missing: {stdout}"

    # Make sure the raw variable references are NOT in the file
    assert "$MY_SECRET_KEY" not in stdout, "Secret reference should be resolved"
    assert "$DATABASE_URL" not in stdout, "Secret reference should be resolved"


def test_sequential_deploys_update_version(
    vps_container, ssh_key, tmp_path, monkeypatch
):
    """Multiple deploys correctly update version and keep history."""
    monkeypatch.chdir(tmp_path)

    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()

    install_dir = tmp_path / ".fujin"
    systemd_dir = install_dir / "systemd"
    systemd_dir.mkdir(parents=True)

    service_file = systemd_dir / "web.service"
    service_file.write_text("""[Unit]
Description=Sequential deploy test

[Service]
Type=simple
ExecStart={install_dir}/seqapp
WorkingDirectory={app_dir}
Restart=always

[Install]
WantedBy=multi-user.target
""")

    def create_config(version: str):
        distfile = dist_dir / f"seqapp-{version}"
        distfile.write_text(
            f"#!/bin/bash\necho 'version {version}' && sleep infinity\n"
        )
        distfile.chmod(0o755)
        return msgspec.convert(
            {
                "app": "seqapp",
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

    # Deploy v1.0.0
    config_v1 = create_config("1.0.0")
    with patch("fujin.config.Config.read", return_value=config_v1):
        deploy = Deploy(no_input=True)
        deploy()

    wait_for_service(vps_container["name"], "seqapp-web.service")

    # Verify v1.0.0 is deployed
    stdout, success = exec_in_container(
        vps_container["name"], "cat /opt/fujin/seqapp/.install/.version"
    )
    assert success and stdout.strip() == "1.0.0"

    # Deploy v1.1.0
    config_v2 = create_config("1.1.0")
    with patch("fujin.config.Config.read", return_value=config_v2):
        deploy = Deploy(no_input=True)
        deploy()

    wait_for_service(vps_container["name"], "seqapp-web.service")

    # Verify v1.1.0 is now deployed
    stdout, success = exec_in_container(
        vps_container["name"], "cat /opt/fujin/seqapp/.install/.version"
    )
    assert success and stdout.strip() == "1.1.0"

    # Verify bundle history exists (both versions available)
    stdout, success = exec_in_container(
        vps_container["name"], "ls /opt/fujin/seqapp/.install/.versions/"
    )
    assert success
    assert "seqapp-1.0.0.pyz" in stdout, f"v1.0.0 bundle should exist: {stdout}"
    assert "seqapp-1.1.0.pyz" in stdout, f"v1.1.0 bundle should exist: {stdout}"

    # Deploy v1.2.0
    config_v3 = create_config("1.2.0")
    with patch("fujin.config.Config.read", return_value=config_v3):
        deploy = Deploy(no_input=True)
        deploy()

    wait_for_service(vps_container["name"], "seqapp-web.service")

    # Verify v1.2.0 is now deployed
    stdout, success = exec_in_container(
        vps_container["name"], "cat /opt/fujin/seqapp/.install/.version"
    )
    assert success and stdout.strip() == "1.2.0"

    # All three versions should be available for rollback
    stdout, success = exec_in_container(
        vps_container["name"], "ls /opt/fujin/seqapp/.install/.versions/"
    )
    assert success
    assert "seqapp-1.0.0.pyz" in stdout
    assert "seqapp-1.1.0.pyz" in stdout
    assert "seqapp-1.2.0.pyz" in stdout


def test_deploy_preserves_app_data_between_versions(
    vps_container, ssh_key, tmp_path, monkeypatch
):
    """Deploys preserve application data in app directory between versions."""
    monkeypatch.chdir(tmp_path)

    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()

    install_dir = tmp_path / ".fujin"
    systemd_dir = install_dir / "systemd"
    systemd_dir.mkdir(parents=True)

    service_file = systemd_dir / "web.service"
    service_file.write_text("""[Unit]
Description=Data persistence test

[Service]
Type=simple
ExecStart={install_dir}/dataapp
WorkingDirectory={app_dir}
Restart=always

[Install]
WantedBy=multi-user.target
""")

    def create_config(version: str):
        distfile = dist_dir / f"dataapp-{version}"
        distfile.write_text(f"#!/bin/bash\nsleep infinity\n")
        distfile.chmod(0o755)
        return msgspec.convert(
            {
                "app": "dataapp",
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

    # Deploy v1.0.0
    config_v1 = create_config("1.0.0")
    with patch("fujin.config.Config.read", return_value=config_v1):
        deploy = Deploy(no_input=True)
        deploy()

    wait_for_service(vps_container["name"], "dataapp-web.service")

    # Create some "application data" that should persist
    exec_in_container(
        vps_container["name"],
        "echo 'user-data-12345' | sudo tee /opt/fujin/dataapp/data.txt",
    )
    exec_in_container(
        vps_container["name"],
        "sudo mkdir -p /opt/fujin/dataapp/uploads && "
        "echo 'uploaded-file' | sudo tee /opt/fujin/dataapp/uploads/file.txt",
    )

    # Deploy v1.1.0
    config_v2 = create_config("1.1.0")
    with patch("fujin.config.Config.read", return_value=config_v2):
        deploy = Deploy(no_input=True)
        deploy()

    wait_for_service(vps_container["name"], "dataapp-web.service")

    # Verify data persisted through deployment
    stdout, success = exec_in_container(
        vps_container["name"], "cat /opt/fujin/dataapp/data.txt"
    )
    assert success and "user-data-12345" in stdout, f"Data file lost: {stdout}"

    stdout, success = exec_in_container(
        vps_container["name"], "cat /opt/fujin/dataapp/uploads/file.txt"
    )
    assert success and "uploaded-file" in stdout, f"Uploads lost: {stdout}"

    # Verify new version is deployed
    stdout, success = exec_in_container(
        vps_container["name"], "cat /opt/fujin/dataapp/.install/.version"
    )
    assert success and stdout.strip() == "1.1.0"
