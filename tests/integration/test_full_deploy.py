"""Integration tests for full deployment workflows.

These tests use Docker containers to simulate a real VPS environment.
"""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import cappa
import pytest

from fujin.commands.app import App
from fujin.commands.doctor import Doctor
from fujin.commands.down import Down
from fujin.commands.rollback import Rollback
from fujin.exit_codes import (
    EXIT_DEPLOY_FAILED,
    EXIT_DEPLOY_ROLLED_BACK,
    EXIT_USER_ABORTED,
)

from .helpers import (
    assert_http_responds,
    assert_noop_redeploy_kept_pid,
    assert_release_layout,
    assert_service_running,
    deploy_app,
    exec_in_container,
    get_current_release,
    get_deployed_version,
    get_main_pid,
    http_server_script,
    release_count,
    wait_for_service,
)


def test_binary_deploy_caddy_and_app_management(
    shared_vps, ssh_key, project, unit, app_port
):
    """Binary deploy with Caddy, no-op redeploy, lifecycle and introspection."""
    container = shared_vps["name"]
    port = app_port
    caddyfile = f"""example.com {{
    handle /static/* {{
        root * /var/www/static/
        file_server
    }}

    handle {{
        reverse_proxy localhost:{port}
    }}
}}
"""
    units = {
        "web.service": unit("fullapp", "web"),
        "worker.service": unit("fullapp", "worker", exec_start="/bin/sleep infinity"),
    }
    config = project(
        "fullapp",
        shared_vps,
        script=http_server_script(port),
        units=units,
        caddyfile=caddyfile,
        env="DEBUG=true\n",
    )

    deploy_app(config)
    wait_for_service(container, "fullapp-web.service")
    wait_for_service(container, "fullapp-worker.service")
    assert_http_responds(container, port)
    assert_release_layout(container, "fullapp")

    # Caddy config was installed and its service reloaded.
    stdout = exec_in_container(container, "cat /etc/caddy/conf.d/fullapp.caddy")[0]
    assert "example.com" in stdout
    assert f"reverse_proxy localhost:{port}" in stdout
    assert "/static/*" in stdout
    assert_service_running(container, "caddy")

    # A second identical deploy is a no-op: same release, same PIDs.
    web_pid = get_main_pid(container, "fullapp-web.service")
    deploy_app(config)
    assert_noop_redeploy_kept_pid(container, "fullapp-web.service", web_pid)
    assert release_count(container, "fullapp") == 1
    assert len(get_current_release(container, "fullapp")) == 64

    # Restart a single service: PID changes.
    with patch("fujin.config.Config.read", return_value=config):
        App().restart(names=["web"])
    wait_for_service(container, "fullapp-web.service")
    new_pid = get_main_pid(container, "fullapp-web.service")
    assert new_pid != web_pid, "PID should change after restart"
    assert new_pid != "0", "Service should have valid PID"

    # Stop and start it again.
    assert_service_running(container, "fullapp-web.service")
    with patch("fujin.config.Config.read", return_value=config):
        App().stop(names=["web"])
    stdout, _ = exec_in_container(container, "systemctl is-active fullapp-web.service")
    assert stdout in ["inactive", "failed"], f"Service should be stopped: {stdout}"
    with patch("fujin.config.Config.read", return_value=config):
        App().start(names=["web"])
    wait_for_service(container, "fullapp-web.service")

    # Restart all services: both PIDs change.
    web_before = get_main_pid(container, "fullapp-web.service")
    worker_before = get_main_pid(container, "fullapp-worker.service")
    with patch("fujin.config.Config.read", return_value=config):
        App().restart(names=None)
    wait_for_service(container, "fullapp-web.service")
    wait_for_service(container, "fullapp-worker.service")
    assert get_main_pid(container, "fullapp-web.service") != web_before
    assert get_main_pid(container, "fullapp-worker.service") != worker_before

    # Introspection: logs, unit cat and env cat all work.
    with patch("fujin.config.Config.read", return_value=config):
        App().logs(names=["web"], lines=10, follow=False)
    stdout, ok = exec_in_container(
        container, "journalctl -u fullapp-web.service -n 5 --no-pager"
    )
    assert ok, f"journalctl should succeed: {stdout}"

    with patch("fujin.config.Config.read", return_value=config):
        App().cat(name="web")
    stdout, ok = exec_in_container(
        container, "systemctl cat fullapp-web.service --no-pager"
    )
    assert ok and "fullapp" in stdout, f"systemctl cat failed: {stdout}"

    with patch("fujin.config.Config.read", return_value=config):
        App().cat(name="env")
    stdout, ok = exec_in_container(container, "cat /opt/fujin/fullapp/current/.env")
    assert ok and "DEBUG=true" in stdout, f"Reading .env failed: {stdout}"


def test_python_package_deploy_resolves_secrets(
    shared_vps, monkeypatch, project, app_port
):
    """Python-package deploy writes a .env with secrets resolved."""
    monkeypatch.setenv("MY_SECRET_KEY", "super-secret-value-123")
    monkeypatch.setenv("DATABASE_URL", "postgres://user:pass@localhost/db")

    container = shared_vps["name"]
    port = app_port
    units = {
        "web.service": f"""[Unit]
Description=Python web

[Service]
Type=simple
ExecStart={{app_dir}}/current/venv/bin/python3 -m http.server {port}
WorkingDirectory={{app_dir}}
EnvironmentFile=-{{app_dir}}/current/.env
Restart=always

[Install]
WantedBy=multi-user.target
""",
        "worker.service": """[Unit]
Description=Python worker

[Service]
Type=simple
ExecStart={app_dir}/current/venv/bin/python3 -c 'import time; time.sleep(99999)'
WorkingDirectory={app_dir}
EnvironmentFile=-{app_dir}/current/.env
Restart=always

[Install]
WantedBy=multi-user.target
""",
    }
    config = project(
        "pyapp",
        shared_vps,
        mode="python-package",
        units=units,
        env=(
            "DEBUG=false\n"
            "SECRET_KEY=$MY_SECRET_KEY\n"
            "DATABASE_URL=$DATABASE_URL\n"
            "STATIC_VALUE=no-secret-here\n"
        ),
    )

    deploy_app(config)
    for service in ["pyapp-web.service", "pyapp-worker.service"]:
        wait_for_service(container, service)

    stdout, ok = exec_in_container(container, "cat /opt/fujin/pyapp/current/.env")
    assert ok, f"Could not read .env file: {stdout}"
    assert "super-secret-value-123" in stdout, f"SECRET_KEY not resolved: {stdout}"
    assert "postgres://user:pass@localhost/db" in stdout, (
        f"DATABASE_URL not resolved: {stdout}"
    )
    assert "no-secret-here" in stdout, f"STATIC_VALUE missing: {stdout}"
    assert "$MY_SECRET_KEY" not in stdout, "Secret reference should be resolved"
    assert "$DATABASE_URL" not in stdout, "Secret reference should be resolved"


def test_version_progression_rollback_and_data(shared_vps, project):
    """Versions advance, releases are kept, rollback restores the previous one
    and application data survives all of it."""
    container = shared_vps["name"]

    def make(version: str):
        return project(
            "rollapp",
            shared_vps,
            version=version,
            script=(f"#!/bin/bash\necho 'version {version}' && sleep infinity\n"),
        )

    deploy_app(make("1.0.0"))
    wait_for_service(container, "rollapp-web.service")
    assert get_deployed_version(container, "rollapp") == "1.0.0"

    deploy_app(make("2.0.0"))
    wait_for_service(container, "rollapp-web.service")
    assert get_deployed_version(container, "rollapp") == "2.0.0"
    assert release_count(container, "rollapp") == 2

    deploy_app(make("3.0.0"))
    wait_for_service(container, "rollapp-web.service")
    assert get_deployed_version(container, "rollapp") == "3.0.0"
    assert release_count(container, "rollapp") == 3

    exec_in_container(
        container,
        "echo 'user-data-12345' | sudo tee /opt/fujin/rollapp/data.txt",
    )
    exec_in_container(
        container,
        "sudo mkdir -p /opt/fujin/rollapp/uploads && "
        "echo 'uploaded-file' | sudo tee /opt/fujin/rollapp/uploads/file.txt",
    )

    # Roll back to the previous kept release (v2).
    with (
        patch("fujin.config.Config.read", return_value=make("3.0.0")),
        patch("fujin.commands.rollback.IntPrompt.ask", return_value=1),
    ):
        Rollback()()

    wait_for_service(container, "rollapp-web.service")
    assert get_deployed_version(container, "rollapp") == "2.0.0"

    stdout, ok = exec_in_container(container, "cat /opt/fujin/rollapp/data.txt")
    assert ok and "user-data-12345" in stdout, f"Data file lost: {stdout}"
    stdout, ok = exec_in_container(container, "cat /opt/fujin/rollapp/uploads/file.txt")
    assert ok and "uploaded-file" in stdout, f"Uploads lost: {stdout}"


def test_failed_service_rollback_modes(shared_vps, project, unit):
    """A failing deploy auto-rolls back (exit 3) or aborts when declined (exit 1)."""
    container = shared_vps["name"]

    def make(app: str, version: str, script: str):
        return project(
            app,
            shared_vps,
            version=version,
            script=script,
            units={"web.service": unit(app, "web", restart="no")},
        )

    # --- automatic rollback (no_input=True) ---
    auto_app = "autofailapp"
    deploy_app(make(auto_app, "1.0.0", "#!/bin/bash\necho 'v1' && sleep infinity\n"))
    wait_for_service(container, f"{auto_app}-web.service")
    assert get_deployed_version(container, auto_app) == "1.0.0"

    broken = make(auto_app, "2.0.0", "#!/bin/bash\nexit 1\n")
    with patch("fujin.config.Config.read", return_value=broken):
        with pytest.raises(cappa.Exit) as excinfo:
            deploy_app(broken)
    assert excinfo.value.code == EXIT_DEPLOY_ROLLED_BACK

    wait_for_service(container, f"{auto_app}-web.service")
    assert get_deployed_version(container, auto_app) == "1.0.0", (
        "Previous release should be live after auto-rollback"
    )

    # --- user declines the rollback offer (exit 1, failed release stays) ---
    decline_app = "declineapp"
    deploy_app(make(decline_app, "1.0.0", "#!/bin/bash\necho 'v1' && sleep infinity\n"))
    wait_for_service(container, f"{decline_app}-web.service")

    broken2 = make(decline_app, "2.0.0", "#!/bin/bash\nexit 1\n")
    with (
        patch("fujin.config.Config.read", return_value=broken2),
        patch(
            "fujin.commands.deploy.Confirm.ask",
            side_effect=lambda prompt, *a, **k: "rollback" not in str(prompt).lower(),
        ),
    ):
        with pytest.raises(cappa.Exit) as excinfo:
            deploy_app(broken2, no_input=False)
    assert excinfo.value.code == EXIT_USER_ABORTED
    assert get_deployed_version(container, decline_app) == "2.0.0", (
        "Declined rollback must leave the failed release current"
    )


def test_post_install_failure_aborts_activation(shared_vps, project):
    """A failing post_install hook exits 4 and leaves the live release untouched."""
    container = shared_vps["name"]
    app = "hookapp"

    deploy_app(
        project(
            app,
            shared_vps,
            version="1.0.0",
            script="#!/bin/bash\necho 'running' && sleep infinity\n",
        )
    )
    wait_for_service(container, f"{app}-web.service")
    live_before = get_current_release(container, app)

    broken = project(
        app,
        shared_vps,
        version="2.0.0",
        script="#!/bin/bash\necho 'running' && sleep infinity\n",
        hooks={"post_install": ["exit 1"]},
    )
    with patch("fujin.config.Config.read", return_value=broken):
        with pytest.raises(cappa.Exit) as excinfo:
            deploy_app(broken)
    assert excinfo.value.code == EXIT_DEPLOY_FAILED

    assert get_current_release(container, app) == live_before
    assert get_deployed_version(container, app) == "1.0.0"
    wait_for_service(container, f"{app}-web.service")


@pytest.mark.no_app_cleanup
def test_down_removes_everything(shared_vps, project):
    """``down`` stops services and removes units, app dir and app user."""
    container = shared_vps["name"]
    config = project("downapp", shared_vps)

    deploy_app(config)
    wait_for_service(container, "downapp-web.service")

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch("fujin.commands.down.Confirm.ask", return_value=True),
    ):
        Down()()

    stdout, _ = exec_in_container(container, "systemctl is-active downapp-web.service")
    assert stdout in ["inactive", "unknown"], (
        f"Expected inactive or unknown, got: {stdout}"
    )
    _, ok = exec_in_container(
        container, "test -f /etc/systemd/system/downapp-web.service"
    )
    assert not ok, "Service file should be removed after down"
    _, ok = exec_in_container(container, "test -d /opt/fujin/downapp")
    assert not ok, "App directory should be removed after down"
    _, ok = exec_in_container(container, "id -u downapp")
    assert not ok, "App user should be deleted after down"


def test_doctor_repairs_missing_symlink_and_stopped_instance(shared_vps, project):
    """``doctor --fix`` repairs a broken ``current`` link and a stopped replica."""
    container = shared_vps["name"]
    config = project(
        "doctorapp",
        shared_vps,
        replicas={"web": 2},
        script="#!/bin/bash\necho 'doctor app' && sleep infinity\n",
    )
    deploy_app(config)

    app_dir = "/opt/fujin/doctorapp"
    for instance in (1, 2):
        wait_for_service(container, f"doctorapp-web@{instance}.service")

    # Break both invariants doctor is meant to repair.
    _, ok = exec_in_container(container, f"rm -f {app_dir}/current")
    assert ok
    _, ok = exec_in_container(container, "systemctl stop doctorapp-web@2.service")
    assert ok

    # Report-only run: both problems are FAILs, nothing is mutated.
    output = MagicMock()
    with (
        patch("fujin.config.Config.read", return_value=config),
        patch.object(Doctor, "output", output),
    ):
        with pytest.raises(cappa.Exit) as excinfo:
            Doctor(fix=False)()
    assert excinfo.value.code == EXIT_USER_ABORTED
    text = "\n".join(call.args[0] for call in output.output.call_args_list)
    assert "current symlink is missing" in text
    assert "start: doctorapp-web@2.service" in text

    # --fix repairs both.
    with (
        patch("fujin.config.Config.read", return_value=config),
        patch.object(Doctor, "output", MagicMock()),
    ):
        Doctor(fix=True)()

    stdout, ok = exec_in_container(container, f"readlink {app_dir}/current")
    assert ok and stdout.strip().startswith("releases/")
    wait_for_service(container, "doctorapp-web@2.service")


def _release_manifest(container: str, app: str, digest: str) -> dict:
    stdout, ok = exec_in_container(
        container, f"cat /opt/fujin/{app}/releases/{digest}/manifest.json"
    )
    assert ok, f"Could not read the manifest of release {digest}: {stdout}"
    return json.loads(stdout)


def _venv_hashes(container: str, app: str, digest: str) -> dict[str, str]:
    """sha256 of every regular file under ``venv/`` of one release."""
    stdout, ok = exec_in_container(
        container,
        f"cd /opt/fujin/{app}/releases/{digest} && "
        "find venv -type f -print0 | sort -z | xargs -0 -r sha256sum",
    )
    assert ok, f"Could not hash the venv of release {digest}: {stdout}"
    return {
        path: digest_
        for digest_, path in (
            line.split("  ", 1) for line in stdout.splitlines() if line
        )
    }


def _venv_entries(manifest: dict) -> dict[str, dict]:
    return {
        path: entry
        for path, entry in manifest["entries"].items()
        if path.startswith("venv/") and entry.get("sha256")
    }


def test_python_package_redeploy_clones_venv_immutably(
    shared_vps, project, app_port, tmp_path
):
    """A redeploy with unchanged requirements clones the live venv.

    The live release must stay byte-identical (hardlinks are never written),
    only the app package is installed into the clone, and the new release
    runs.
    """
    container = shared_vps["name"]
    port = app_port
    units = {
        "web.service": f"""[Unit]
Description=Python web

[Service]
Type=simple
ExecStart={{app_dir}}/current/venv/bin/python3 -m http.server {port}
WorkingDirectory={{app_dir}}
Restart=always

[Install]
WantedBy=multi-user.target
""",
    }

    def make(version: str):
        config = project(
            "cloneapp",
            shared_vps,
            mode="python-package",
            version=version,
            units=units,
        )
        # A (comment-only) requirements file: its sha256 is the identity of
        # the dependency set the venv clone check compares. An empty file is
        # not staged as an entry at all, which disables cloning by design.
        (tmp_path / "requirements.txt").write_text("# no runtime dependencies\n")
        return config

    started = time.monotonic()
    deploy_app(make("1.0.0"))
    v1_seconds = time.monotonic() - started
    wait_for_service(container, "cloneapp-web.service")
    assert_http_responds(container, port)

    v1_digest = get_current_release(container, "cloneapp")
    v1_manifest = _release_manifest(container, "cloneapp", v1_digest)
    v1_entries = _venv_entries(v1_manifest)
    assert v1_entries, "the first python-package deploy must record venv/**"
    v1_hashes = _venv_hashes(container, "cloneapp", v1_digest)
    assert set(v1_hashes) == set(v1_entries)
    for path, entry in v1_entries.items():
        assert entry["sha256"] == v1_hashes[path], path

    # The requirements file is byte-identical, only the wheel changed.
    started = time.monotonic()
    deploy_app(make("2.0.0"))
    v2_seconds = time.monotonic() - started
    wait_for_service(container, "cloneapp-web.service")
    assert_http_responds(container, port)

    v2_digest = get_current_release(container, "cloneapp")
    assert v2_digest != v1_digest

    # Immutability: every v1 venv file hashes exactly as the v1 manifest says.
    v1_after = _venv_hashes(container, "cloneapp", v1_digest)
    assert v1_after == v1_hashes
    for path, entry in v1_entries.items():
        assert v1_after[path] == entry["sha256"], path

    # The clone really was hardlinked from v1: at least one venv file is the
    # same inode in both releases (and therefore was never re-read/re-written).
    _release_manifest(container, "cloneapp", v2_digest)
    v2_hashes = _venv_hashes(container, "cloneapp", v2_digest)
    shared = [
        path
        for path in v1_hashes
        if path in v2_hashes and v1_hashes[path] == v2_hashes[path]
    ]
    assert shared, "expected unchanged venv files shared between releases"
    stats, ok = exec_in_container(
        container,
        f"stat -c '%i %d' /opt/fujin/cloneapp/releases/{v1_digest}/{shared[0]}"
        f" /opt/fujin/cloneapp/releases/{v2_digest}/{shared[0]}",
    )
    assert ok, stats
    inode_v1, dev_v1, inode_v2, dev_v2 = stats.split()
    assert (inode_v1, dev_v1) == (inode_v2, dev_v2), shared[0]

    print(
        f"\npython-package deploy timings: v1 (full install)={v1_seconds:.2f}s,"
        f" v2 (venv clone)={v2_seconds:.2f}s"
    )
