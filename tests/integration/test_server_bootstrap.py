"""Integration tests for server bootstrap and setup commands.

These tests verify that server bootstrap, user management and hardening work
correctly.  They mutate global server state and therefore each get a fresh
container.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from fujin.commands.server import Server

from .helpers import (
    assert_app_user_exists,
    assert_dir_exists,
    assert_file_exists,
    assert_sudo_scoped,
    deploy_app,
    exec_in_container,
    http_server_script,
    wait_for_service,
)

pytestmark = pytest.mark.no_app_cleanup


def test_bootstrap_layout_and_caddy_dir(fresh_vps_container, project):
    """Bootstrap creates the release layout, permissions and is idempotent."""
    container = fresh_vps_container["name"]
    user = fresh_vps_container["user"]
    config = project(
        "testapp",
        fresh_vps_container,
        caddyfile="example.com {\n    reverse_proxy localhost:8000\n}\n",
    )

    with patch("fujin.config.Config.read", return_value=config):
        Server().bootstrap()
        # Second run must be a no-op and still succeed.
        Server().bootstrap()

    stdout, ok = exec_in_container(container, "getent group fujin")
    assert ok, f"fujin group should exist: {stdout}"

    assert_dir_exists(container, "/opt/fujin")
    assert_dir_exists(container, "/opt/fujin/.python")
    stdout, _ = exec_in_container(container, "stat -c '%G' /opt/fujin")
    assert stdout == "fujin", f"/opt/fujin should be owned by fujin group: {stdout}"
    for path in ("/opt/fujin", "/opt/fujin/.python"):
        stdout, _ = exec_in_container(container, f"stat -c '%a' {path}")
        assert stdout == "775", f"{path} should have 775 permissions: {stdout}"

    stdout, ok = exec_in_container(container, f"groups {user}")
    assert ok and "fujin" in stdout, f"User should be in fujin group: {stdout}"
    stdout, ok = exec_in_container(
        container,
        f"test -f /home/{user}/.local/bin/uv && echo 'found'",
    )
    assert ok and "found" in stdout, "uv should be installed in ~/.local/bin"

    # Release-tree layout with setgid dirs and the store lock.
    app_dir = "/opt/fujin/testapp"
    assert_dir_exists(container, f"{app_dir}/releases")
    assert_dir_exists(container, f"{app_dir}/store")
    assert_dir_exists(container, f"{app_dir}/shared")
    assert_file_exists(container, f"{app_dir}/store/lock")
    for path in (app_dir, f"{app_dir}/releases", f"{app_dir}/store"):
        stdout, _ = exec_in_container(container, f"stat -c '%a' {path}")
        assert stdout == "2755", f"{path} should be setgid 2755, got: {stdout}"
    stdout, _ = exec_in_container(container, f"stat -c '%a' {app_dir}/shared")
    assert stdout == "2775", f"shared/ should be 2775, got: {stdout}"

    # Caddy config directory (Caddy itself is preinstalled in the image).
    assert_dir_exists(container, "/etc/caddy/conf.d")


def test_create_user_then_deploy_as_that_user(fresh_vps_container, project, app_port):
    """create-user grants scoped sudo + SSH access (and optionally a password),
    then a deploy works with the new user's credentials only."""
    container = fresh_vps_container["name"]
    port = app_port
    app = "scopedapp"
    script = http_server_script(port)

    admin_config = project(app, fresh_vps_container, script=script)
    with patch("fujin.config.Config.read", return_value=admin_config):
        Server().bootstrap()
        Server().create_user(name="newdeploy", with_password=True)

    # SSH access for the new deploy user.
    assert_app_user_exists(container, "newdeploy")
    assert_dir_exists(container, "/home/newdeploy/.ssh")
    assert_file_exists(container, "/home/newdeploy/.ssh/authorized_keys")
    stdout, _ = exec_in_container(container, "stat -c '%a' /home/newdeploy/.ssh")
    assert stdout == "700", f".ssh should have 700 permissions: {stdout}"
    stdout, _ = exec_in_container(
        container, "stat -c '%a' /home/newdeploy/.ssh/authorized_keys"
    )
    assert stdout == "600", f"authorized_keys should have 600 permissions: {stdout}"

    # Scoped sudo rule, no NOPASSWD: ALL anywhere.
    assert_sudo_scoped(container, "newdeploy", f"/opt/fujin/{app}")

    # --with-password actually sets a usable password.
    stdout, _ = exec_in_container(
        container, "sudo getent shadow newdeploy | cut -d: -f2"
    )
    assert stdout and stdout not in ["", "*", "!", "!!"], (
        f"User should have a password set: {stdout}"
    )

    # Deploy as the new user, using the same key (copied by create-user).
    deploy_config = project(app, fresh_vps_container, script=script, user="newdeploy")
    deploy_app(deploy_config)

    stdout, ok = exec_in_container(container, f"systemctl is-active {app}-web.service")
    assert ok and stdout == "active", f"Service not active: {stdout}"
    stdout, _ = exec_in_container(container, f"sudo cat /opt/fujin/.audit/{app}.log")
    assert '"operation": "deploy"' in stdout


def test_harden_then_deploy_still_works(fresh_vps_container, project):
    """harden replaces NOPASSWD:ALL in one transaction; deploy still works."""
    container = fresh_vps_container["name"]
    user = fresh_vps_container["user"]
    app = "hardenapp"

    config = project(app, fresh_vps_container)
    with patch("fujin.config.Config.read", return_value=config):
        Server().bootstrap()
        Server().harden()

    # The legacy line is gone, the drop-in is present.
    stdout, _ = exec_in_container(
        container,
        f"grep -c '{user}.*NOPASSWD:[ ]*ALL' /etc/sudoers || true",
    )
    assert stdout.strip() in ("", "0")
    stdout, _ = exec_in_container(container, f"sudo cat /etc/sudoers.d/fujin-{user}")
    assert "FUJIN_DEPLOY" in stdout
    stdout, _ = exec_in_container(container, f"sudo -l -U {user}")
    # ``sudo -l`` expands aliases; the app-scoped rule is visible instead.
    assert f"/opt/fujin/{app}" in stdout
    assert "NOPASSWD: ALL" not in stdout

    deploy_app(config)
    wait_for_service(container, f"{app}-web.service")
