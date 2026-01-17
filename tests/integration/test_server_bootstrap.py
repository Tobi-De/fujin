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

    # Verify sudo access
    stdout, _ = exec_in_container(
        vps_container["name"], "grep 'newdeploy.*NOPASSWD' /etc/sudoers"
    )
    assert "newdeploy" in stdout, "User should have NOPASSWD sudo access"


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
