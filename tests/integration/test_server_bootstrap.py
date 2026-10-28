"""Integration tests for server bootstrap and setup commands.

These tests verify that server bootstrap and user management work correctly.
"""

from __future__ import annotations

from unittest.mock import patch

import msgspec

from fujin.commands.server import Server
from fujin.config import Config

from .helpers import (
    assert_dir_exists,
    assert_file_exists,
    assert_service_running,
    exec_in_container,
)


def test_bootstrap_creates_infrastructure(
    vps_container, ssh_key, tmp_path, monkeypatch
):
    """Bootstrap creates group, directories, permissions and is idempotent."""
    monkeypatch.chdir(tmp_path)

    config_dict = {
        "app": "testapp",
        "version": "1.0.0",
        "build_command": "echo 'building'",
        "distfile": "dist/testapp-{version}",
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

    # Run bootstrap
    with patch("fujin.config.Config.read", return_value=config):
        server = Server()
        server.bootstrap()

    # Verify fujin group exists
    stdout, success = exec_in_container(vps_container["name"], "getent group fujin")
    assert success, f"fujin group should exist: {stdout}"

    # Verify /opt/fujin directory exists with correct permissions
    assert_dir_exists(vps_container["name"], "/opt/fujin")
    assert_dir_exists(vps_container["name"], "/opt/fujin/.python")

    # Verify directory is group-owned by fujin
    stdout, _ = exec_in_container(vps_container["name"], "stat -c '%G' /opt/fujin")
    assert stdout == "fujin", f"/opt/fujin should be owned by fujin group: {stdout}"

    # Verify /opt/fujin permissions (775)
    stdout, _ = exec_in_container(vps_container["name"], "stat -c '%a' /opt/fujin")
    assert stdout == "775", f"/opt/fujin should have 775 permissions: {stdout}"

    # Verify /opt/fujin/.python permissions
    stdout, _ = exec_in_container(
        vps_container["name"], "stat -c '%a' /opt/fujin/.python"
    )
    assert stdout == "775", f"/opt/fujin/.python should have 775 permissions: {stdout}"

    # Verify deploy user is in fujin group
    stdout, success = exec_in_container(
        vps_container["name"], f"groups {vps_container['user']}"
    )
    assert success
    assert "fujin" in stdout, f"User should be in fujin group: {stdout}"

    # Verify uv is available in expected location
    stdout, success = exec_in_container(
        vps_container["name"],
        f"test -f /home/{vps_container['user']}/.local/bin/uv && echo 'found'",
    )
    assert success and "found" in stdout, "uv should be installed in ~/.local/bin"

    # Run again - should be idempotent
    with patch("fujin.config.Config.read", return_value=config):
        server.bootstrap()

    # Verify the release-tree layout: setgid dirs + store lock
    assert_dir_exists(vps_container["name"], "/opt/fujin/testapp/releases")
    assert_dir_exists(vps_container["name"], "/opt/fujin/testapp/store")
    assert_dir_exists(vps_container["name"], "/opt/fujin/testapp/shared")
    _, ok = exec_in_container(
        vps_container["name"], "test -f /opt/fujin/testapp/store/lock"
    )
    assert ok, "store/lock does not exist after bootstrap"
    for path in (
        "/opt/fujin/testapp",
        "/opt/fujin/testapp/releases",
        "/opt/fujin/testapp/store",
    ):
        stdout, _ = exec_in_container(vps_container["name"], f"stat -c '%a' {path}")
        assert stdout == "2755", f"{path} should be setgid 2755, got: {stdout}"
    stdout, _ = exec_in_container(
        vps_container["name"], "stat -c '%a' /opt/fujin/testapp/shared"
    )
    assert stdout == "2775", f"shared/ should be 2775, got: {stdout}"

    # Verify everything still works after second run
    assert_dir_exists(vps_container["name"], "/opt/fujin")
    stdout, success = exec_in_container(vps_container["name"], "getent group fujin")
    assert success


def test_bootstrap_sets_up_caddy_when_caddyfile_exists(
    vps_container, ssh_key, tmp_path, monkeypatch
):
    """Bootstrap installs Caddy when Caddyfile exists in project."""
    monkeypatch.chdir(tmp_path)

    # Create .fujin directory with Caddyfile
    install_dir = tmp_path / ".fujin"
    install_dir.mkdir()
    caddyfile = install_dir / "Caddyfile"
    caddyfile.write_text("""example.com {
    reverse_proxy localhost:8000
}
""")

    config_dict = {
        "app": "webtest",
        "version": "1.0.0",
        "build_command": "echo 'building'",
        "distfile": "dist/webtest-{version}",
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
        server = Server()
        server.bootstrap()

    # Verify Caddy service is running (mocked in Docker but should be active)
    assert_service_running(vps_container["name"], "caddy")

    # Verify Caddy config directory exists
    assert_dir_exists(vps_container["name"], "/etc/caddy/conf.d")


def test_create_user_creates_user_with_ssh_access(
    vps_container, ssh_key, tmp_path, monkeypatch
):
    """create-user command creates a new user with SSH and sudo access."""
    monkeypatch.chdir(tmp_path)

    config_dict = {
        "app": "usertest",
        "version": "1.0.0",
        "build_command": "echo 'building'",
        "distfile": "dist/usertest-{version}",
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
        server = Server()
        server.create_user(name="newdeploy", with_password=False)

    # Verify user was created
    stdout, success = exec_in_container(vps_container["name"], "id -u newdeploy")
    assert success, f"User newdeploy should exist: {stdout}"

    # Verify .ssh directory exists
    assert_dir_exists(vps_container["name"], "/home/newdeploy/.ssh")

    # Verify authorized_keys was copied
    assert_file_exists(vps_container["name"], "/home/newdeploy/.ssh/authorized_keys")

    # Verify .ssh permissions
    stdout, _ = exec_in_container(
        vps_container["name"], "stat -c '%a' /home/newdeploy/.ssh"
    )
    assert stdout == "700", f".ssh should have 700 permissions: {stdout}"

    # Verify authorized_keys permissions
    stdout, _ = exec_in_container(
        vps_container["name"], "stat -c '%a' /home/newdeploy/.ssh/authorized_keys"
    )
    assert stdout == "600", f"authorized_keys should have 600 permissions: {stdout}"

    # Verify the scoped sudoers drop-in (and no NOPASSWD:ALL anywhere)
    stdout, _ = exec_in_container(
        vps_container["name"], "sudo cat /etc/sudoers.d/fujin-newdeploy"
    )
    assert "newdeploy ALL=(root) NOPASSWD: FUJIN_DEPLOY" in stdout
    assert "NOPASSWD:ALL" not in stdout
    stdout, _ = exec_in_container(
        vps_container["name"],
        "grep -c 'newdeploy.*NOPASSWD:ALL' /etc/sudoers || true",
    )
    assert stdout.strip() in ("", "0"), "NOPASSWD:ALL must not be granted"


def test_create_user_with_password_generates_password(
    vps_container, ssh_key, tmp_path, monkeypatch, capsys
):
    """create-user with --with-password generates and sets a password."""
    monkeypatch.chdir(tmp_path)

    config_dict = {
        "app": "passtest",
        "version": "1.0.0",
        "build_command": "echo 'building'",
        "distfile": "dist/passtest-{version}",
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
        server = Server()
        server.create_user(name="passuser", with_password=True)

    # Verify user was created
    stdout, success = exec_in_container(vps_container["name"], "id -u passuser")
    assert success, f"User passuser should exist: {stdout}"

    # Verify user has a password set (password field is not empty/locked)
    stdout, _ = exec_in_container(
        vps_container["name"], "sudo getent shadow passuser | cut -d: -f2"
    )
    # Password should not be empty, * or !
    assert stdout and stdout not in ["", "*", "!", "!!"], (
        f"User should have a password set: {stdout}"
    )


# ============================================================================
# Scoped sudo rule end-to-end (issue #93)
# ============================================================================


def _write_scoped_app(tmp_path, app: str, port: int) -> dict:
    """A minimal binary app + fujin config dict for deploy tests."""
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    distfile = dist_dir / f"{app}-1.0.0"
    distfile.write_text(
        "#!/usr/bin/env python3\n"
        "import http.server\n"
        "import socketserver\n"
        "\n"
        f"PORT = {port}\n"
        "Handler = http.server.SimpleHTTPRequestHandler\n"
        "with socketserver.TCPServer(('', PORT), Handler) as httpd:\n"
        "    httpd.serve_forever()\n"
    )
    distfile.chmod(0o755)

    systemd_dir = tmp_path / ".fujin" / "systemd"
    systemd_dir.mkdir(parents=True)
    (systemd_dir / "web.service").write_text(
        "[Unit]\n"
        f"Description={app} web\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={{app_dir}}/current/bin/{app}\n"
        "WorkingDirectory={app_dir}\n"
        "Restart=always\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )
    return {
        "app": app,
        "version": "1.0.0",
        "build_command": "echo building",
        "distfile": str(distfile),
        "installation_mode": "binary",
    }


def test_scoped_create_user_then_deploy_as_that_user(
    vps_container, ssh_key, tmp_path, monkeypatch
):
    """Fresh box: create-user, then deploy as the new user with only the rule."""
    from fujin.commands.deploy import Deploy

    monkeypatch.chdir(tmp_path)
    app = "scopedapp"
    app_config = _write_scoped_app(tmp_path, app, 8000)

    admin_config = msgspec.convert(
        {
            **app_config,
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

    with patch("fujin.config.Config.read", return_value=admin_config):
        Server().bootstrap()
        Server().create_user(name="newdeploy", with_password=False)

    # The new user has the scoped drop-in and no NOPASSWD:ALL of its own.
    stdout, _ = exec_in_container(
        vps_container["name"], "sudo cat /etc/sudoers.d/fujin-newdeploy"
    )
    assert "newdeploy ALL=(root) NOPASSWD: FUJIN_DEPLOY" in stdout
    assert f"/opt/fujin/{app}" in stdout
    stdout, _ = exec_in_container(
        vps_container["name"],
        "grep -c 'newdeploy.*NOPASSWD:ALL' /etc/sudoers || true",
    )
    assert stdout.strip() in ("", "0")

    # Deploy with the new user's SSH credentials only.
    deploy_config = msgspec.convert(
        {
            **app_config,
            "hosts": [
                {
                    "address": vps_container["ip"],
                    "user": "newdeploy",
                    "port": vps_container["port"],
                    "key_filename": ssh_key,
                }
            ],
        },
        type=Config,
    )
    with patch("fujin.config.Config.read", return_value=deploy_config):
        Deploy(no_input=True)()

    stdout, success = exec_in_container(
        vps_container["name"], f"systemctl is-active {app}-web.service"
    )
    assert success, f"Service not active: {stdout}"
    assert stdout == "active"
    stdout, _ = exec_in_container(
        vps_container["name"], "sudo cat /opt/fujin/.audit/scopedapp.log"
    )
    assert '"operation": "deploy"' in stdout


def test_harden_nopasswd_all_box_then_deploy_still_works(
    vps_container, ssh_key, tmp_path, monkeypatch
):
    """harden replaces NOPASSWD:ALL in one transaction; deploy still works."""
    from fujin.commands.deploy import Deploy

    monkeypatch.chdir(tmp_path)
    app = "hardenapp"
    app_config = _write_scoped_app(tmp_path, app, 8000)
    config = msgspec.convert(
        {
            **app_config,
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

    with patch("fujin.config.Config.read", return_value=config):
        Server().bootstrap()
        Server().harden()

    # The legacy line is gone, the drop-in is present, and the listing still
    # grants the alias without NOPASSWD: ALL.
    stdout, _ = exec_in_container(
        vps_container["name"],
        f"grep -c '{vps_container['user']}.*NOPASSWD:[ ]*ALL' /etc/sudoers || true",
    )
    assert stdout.strip() in ("", "0")
    stdout, _ = exec_in_container(
        vps_container["name"],
        f"sudo cat /etc/sudoers.d/fujin-{vps_container['user']}",
    )
    assert "FUJIN_DEPLOY" in stdout
    stdout, _ = exec_in_container(
        vps_container["name"],
        f"sudo -l -U {vps_container['user']}",
    )
    assert "FUJIN_DEPLOY" in stdout
    assert "NOPASSWD: ALL" not in stdout

    with patch("fujin.config.Config.read", return_value=config):
        Deploy(no_input=True)()

    stdout, success = exec_in_container(
        vps_container["name"], f"systemctl is-active {app}-web.service"
    )
    assert success, f"Service not active: {stdout}"
    assert stdout == "active"
