"""Tests for fujin.serverops (client-driven server operations)."""

from __future__ import annotations

import base64
import json
from unittest.mock import MagicMock

import pytest

from fujin import serverops
from fujin.store import State


@pytest.fixture
def conn():
    """MagicMock connection defaulted to succeed, recording all commands."""
    c = MagicMock()
    c.run.return_value = ("", True)
    return c


# =============================================================================
# ensure_layout
# =============================================================================


def test_ensure_layout_command(conn):
    serverops.ensure_layout(conn, "/opt/fujin/myapp", "myapp", "deploy")
    cmd = conn.run.call_args[0][0]
    assert (
        "sudo mkdir -p /opt/fujin/myapp/releases /opt/fujin/myapp/store /opt/fujin/myapp/shared"
        in cmd
    )
    assert (
        "sudo chmod 2755 /opt/fujin/myapp /opt/fujin/myapp/releases /opt/fujin/myapp/store"
        in cmd
    )
    assert "sudo chmod 2775 /opt/fujin/myapp/shared" in cmd
    assert "store/lock" in cmd
    assert "chown deploy:myapp" in cmd
    # never touches legacy dirs
    assert ".install" not in cmd
    assert ".versions" not in cmd


def test_ensure_layout_idempotent(conn):
    serverops.ensure_layout(conn, "/opt/fujin/myapp", "myapp", "deploy")
    cmd = conn.run.call_args[0][0]
    assert "mkdir -p" in cmd  # -p is the idempotence marker
    assert "touch" in cmd


def test_ensure_layout_failure(conn):
    conn.run.return_value = ("chmod: error", False)
    with pytest.raises(serverops.ServerOpsError):
        serverops.ensure_layout(conn, "/opt/fujin/myapp", "myapp", "deploy")


# =============================================================================
# Deploy lock
# =============================================================================


def test_acquire_lock_fallback_noclobber(conn):
    conn.run.side_effect = [
        ("/usr/bin/flock", False),  # command -v flock missing
        ("", True),  # noclobber create
    ]
    token = serverops.acquire_lock(conn, "/opt/fujin/myapp")
    assert token == "file:/opt/fujin/myapp/.deploy_lock"
    create_cmd = conn.run.call_args_list[1][0][0]
    assert "set -C" in create_cmd and "echo $$ >" in create_cmd


def test_acquire_lock_busy_fallback_raises(conn):
    conn.run.side_effect = [
        ("", False),  # no flock
        ("", False),  # noclobber fails -> busy
    ]
    with pytest.raises(serverops.ServerOpsError, match="Another deployment"):
        serverops.acquire_lock(conn, "/opt/fujin/myapp")


def test_acquire_lock_flock_holder(conn):
    conn.run.side_effect = [
        ("/usr/bin/flock", True),  # flock available
        ("", True),  # holder spawn
        ("HELD-12345-TOK1\nALIVE-12345", True),  # probe: our live holder
    ]
    import fujin.serverops as _so

    prev = _so.secrets.token_hex
    _so.secrets.token_hex = lambda _n: "TOK1"
    try:
        token = serverops.acquire_lock(conn, "/opt/fujin/myapp", ttl=7200)
    finally:
        _so.secrets.token_hex = prev
    assert token == "flock:12345"
    holder_cmd = conn.run.call_args_list[1][0][0]
    assert "flock -x" in holder_cmd and "sleep 7200" in holder_cmd


def _holder_token(conn) -> str:
    """Extract the random lock token from the recorded holder command."""
    holder_cmd = conn.run.call_args_list[1][0][0]
    return holder_cmd.split("FUJIN_LOCK_TOKEN=")[1].split(" ")[0]


def test_acquire_lock_foreign_live_holder_raises(conn):
    conn.run.side_effect = [
        ("/usr/bin/flock", True),
        ("", True),  # holder spawn
        ("HELD-999\nALIVE-999", True),  # foreign live holder (different token)
    ]
    with pytest.raises(serverops.ServerOpsError, match="Another deployment"):
        serverops.acquire_lock(conn, "/opt/fujin/myapp")
    # must NOT fall back to the noclobber belt
    assert not any("set -C" in c[0][0] for c in conn.run.call_args_list)


def test_acquire_lock_stale_holder_is_stolen(conn, monkeypatch):
    monkeypatch.setattr(serverops.secrets, "token_hex", lambda _n: "TOK1")
    conn.run.side_effect = [
        ("/usr/bin/flock", True),
        ("", True),  # holder spawn
        ("HELD-999-TOK9\nDEAD", True),  # dead pid -> stale, steal
        ("", True),  # cleanup kills
        ("", True),  # rm holder file
        ("", True),  # retried holder spawn
        ("HELD-222-TOK1\nALIVE-222", True),  # acquired on retry
    ]
    token = serverops.acquire_lock(conn, "/opt/fujin/myapp")
    assert token == "flock:222"


def test_acquire_lock_flock_free_falls_back(conn):
    conn.run.side_effect = [
        ("/usr/bin/flock", True),
        ("", True),  # holder spawn
        ("FREE", True),  # probe: lock is free, so we don't hold it
        ("", True),  # kill holder
        ("", True),  # rm holder file
        ("", True),  # noclobber
    ]
    token = serverops.acquire_lock(conn, "/opt/fujin/myapp")
    assert token == "file:/opt/fujin/myapp/.deploy_lock"


def test_acquire_lock_stale_persists_raises(conn, monkeypatch):
    monkeypatch.setattr(serverops.secrets, "token_hex", lambda _n: "TOK1")
    conn.run.side_effect = [
        ("/usr/bin/flock", True),
        ("", True),
        ("HELD-999-DEAD\nDEAD", True),
        ("", True),
        ("", True),
        ("", True),
        ("HELD-999-DEAD\nDEAD", True),  # stale again on retry
        ("", True),
        ("", True),
    ]
    with pytest.raises(serverops.ServerOpsError, match="stale holder"):
        serverops.acquire_lock(conn, "/opt/fujin/myapp")


def test_parse_holder_pid():
    assert serverops.parse_holder_pid("HELD-42") == "42"
    assert serverops.parse_holder_pid("HELD-?") is None
    assert serverops.parse_holder_pid("FREE") is None


def test_release_lock_flock(conn):
    serverops.release_lock(conn, "/opt/fujin/myapp", "flock:42")
    cmds = [c[0][0] for c in conn.run.call_args_list]
    assert any("kill 42" in c for c in cmds)


def test_release_lock_file(conn):
    serverops.release_lock(
        conn, "/opt/fujin/myapp", "file:/opt/fujin/myapp/.deploy_lock"
    )
    assert "rm -f /opt/fujin/myapp/.deploy_lock" in conn.run.call_args[0][0]


# =============================================================================
# install_code_into_staging
# =============================================================================


def test_install_code_python(conn, tmp_path):
    dist = tmp_path / "myapp-1.0-py3-none-any.whl"
    dist.write_bytes(b"wheel")
    req = tmp_path / "requirements.txt"
    req.write_text("flask\n")

    serverops.install_code_into_staging(
        conn,
        "/opt/fujin/myapp/releases/.staging-1",
        "python-package",
        "3.11",
        str(req),
        "bin/myapp-1.0-py3-none-any.whl",
        app_name="myapp",
    )
    # dist is part of the rsynced staging tree; only requirements is uploaded
    uploaded = {c.args[0]: c.args[1] for c in conn.put.call_args_list}
    assert uploaded == {
        str(req): "/opt/fujin/myapp/releases/.staging-1/requirements.txt"
    }

    cmds = [c[0][0] for c in conn.run.call_args_list]
    assert any(
        "uv venv --relocatable -p 3.11 --managed-python" in c and "staging-1/venv" in c
        for c in cmds
    )
    assert any(
        "uv pip install --python" in c
        and "--no-deps" in c
        and "staging-1/bin/myapp-1.0-py3-none-any.whl" in c
        and "-r /opt/fujin/myapp/releases/.staging-1/requirements.txt" in c
        and "UV_COMPILE_BYTECODE=1" in c
        and "UV_PYTHON_INSTALL_DIR=/opt/fujin/.python" in c
        for c in cmds
    )
    # no transient distfile upload/cleanup exists anymore
    assert not any(".distfile" in c for c in cmds)


def test_install_code_python_venv_never_live(conn):
    serverops.install_code_into_staging(
        conn,
        "/opt/fujin/myapp/releases/.staging-1",
        "python-package",
        "3.12",
        None,
        "bin/dist.whl",
        app_name="myapp",
    )
    for c in conn.run.call_args_list:
        # target is always staging/venv, never .install/.venv
        assert ".install" not in c[0][0]


def test_install_code_binary(conn, tmp_path):
    dist = tmp_path / "myapp"
    dist.write_bytes(b"\x7fELF...")
    serverops.install_code_into_staging(
        conn,
        "/opt/fujin/myapp/releases/.staging-7",
        "binary",
        None,
        None,
        "bin/myapp",
        app_name="myapp",
    )
    # dist ships via the rsynced staging tree; no per-file upload anymore
    assert conn.put.call_args_list == []
    assert any(
        "chmod 755" in c[0][0] and "staging-7/bin/myapp" in c[0][0]
        for c in conn.run.call_args_list
    )


def test_install_code_unknown_mode(conn):
    with pytest.raises(serverops.ServerOpsError, match="Unknown installation mode"):
        serverops.install_code_into_staging(
            conn,
            "/tmp/staging",
            "docker",
            None,
            None,
            "bin/d",
            app_name="x",
        )


def test_install_code_failure_raises(conn, tmp_path):
    conn.run.side_effect = [("error out", False)]
    with pytest.raises(serverops.ServerOpsError, match="venv"):
        serverops.install_code_into_staging(
            conn,
            "/s",
            "python-package",
            "3.11",
            None,
            "bin/myapp-1.0-py3-none-any.whl",
            app_name="x",
        )


# =============================================================================
# hash_server_built
# =============================================================================


def test_hash_server_built_parses_json(conn):
    payload = json.dumps(
        {
            "venv/bin/app": {"sha256": "a" * 64, "mode": 420},
            "venv/bin/python": {
                "link": "/opt/fujin/.python/cpython-3.11/bin/python3",
                "mode": 0o777,
            },
        }
    )
    conn.run.return_value = (payload, True)
    result = serverops.hash_server_built(conn, "/s", "python-package")
    assert result["venv/bin/app"] == {"sha256": "a" * 64, "mode": 420}
    assert result["venv/bin/python"]["link"].endswith("python3")

    # hashing script walks the right root
    script = conn.run.call_args[0][0]
    assert "root = '/s/venv'" in script
    assert "hashlib" in script and "os.walk" in script


def test_hash_server_built_binary_root(conn):
    conn.run.return_value = ("{}", True)
    serverops.hash_server_built(conn, "/s", "binary")
    assert "root = '/s/bin'" in conn.run.call_args[0][0]


def test_hash_server_built_bad_json(conn):
    conn.run.return_value = ("not json", True)
    with pytest.raises(serverops.ServerOpsError, match="Undecodable"):
        serverops.hash_server_built(conn, "/s", "binary")


def test_hash_server_built_command_fails(conn):
    conn.run.return_value = ("", False)
    with pytest.raises(serverops.ServerOpsError, match="hashing"):
        serverops.hash_server_built(conn, "/s", "python-package")


# =============================================================================
# install_units
# =============================================================================


def test_install_units_writes_and_reloads(conn):
    serverops.install_units(
        conn,
        "myapp",
        {"myapp-web@.service": "[Service]\n", "myapp-web.socket": "[Socket]\n"},
        {"myapp-web@.service": {"limits.conf": "[Service]\nLimitNOFILE=8192\n"}},
    )
    cmds = [c[0][0] for c in conn.run.call_args_list]
    assert any(
        "sudo tee" in c and "/etc/systemd/system/myapp-web@.service" in c for c in cmds
    )
    assert any("/etc/systemd/system/myapp-web.socket" in c for c in cmds)
    assert any("myapp-web@.service.d/limits.conf" in c for c in cmds)
    assert cmds[-1] == "sudo systemctl daemon-reload"
    # content travels base64-encoded (quoting safe)
    assert base64.b64encode(b"[Service]\n").decode() in "\n".join(cmds)


def test_install_units_stale_cleanup(conn):
    conn.run.side_effect = lambda cmd, **kw: (
        (
            "/etc/systemd/system/myapp-old.service\n"
            "/etc/systemd/system/myapp-web@.service\n"
            "/etc/systemd/system/myapp-gone@.service.d\n",
            True,
        )
        if "ls -1 /etc/systemd/system/" in cmd
        else ("", True)
    )
    serverops.install_units(conn, "myapp", {"myapp-web@.service": "[Unit]\n"}, {})
    ls_cmd = conn.run.call_args_list[0][0][0]
    # glob stays OUTSIDE the quotes so the remote shell expands it (shlex
    # leaves a safe name unquoted; special chars would be quoted before the *)
    assert ls_cmd.startswith("ls -1 /etc/systemd/system/myapp*")
    assert "*'" not in ls_cmd  # the glob is never inside a quoted segment
    assert "|| true" in ls_cmd
    cmds = [c[0][0] for c in conn.run.call_args_list]
    assert any("disable --now --quiet myapp-old.service" in c for c in cmds)
    assert any(
        "reset-failed myapp-old.service myapp-gone@.service.d" in c for c in cmds
    )
    assert any(
        "sudo rm -rf /etc/systemd/system/myapp-old.service "
        "/etc/systemd/system/myapp-gone@.service.d" in c
        for c in cmds
    )


def test_install_units_no_stale_units(conn):
    conn.run.side_effect = lambda cmd, **kw: ("", True)  # ls matches nothing
    serverops.install_units(conn, "myapp", {"myapp-web@.service": "[Unit]\n"}, {})
    cmds = [c[0][0] for c in conn.run.call_args_list]
    assert not any("systemctl disable" in c for c in cmds)
    assert not any("rm -rf" in c for c in cmds)


def test_install_units_daemon_reload_failure(conn):
    conn.run.side_effect = lambda cmd, **kw: (
        ("", True) if cmd != "sudo systemctl daemon-reload" else ("", False)
    )
    with pytest.raises(serverops.ServerOpsError, match="daemon-reload"):
        serverops.install_units(conn, "myapp", {"myapp-web.service": "[Unit]\n"}, {})


def test_write_file_failure_raises(conn):
    conn.run.return_value = ("", False)
    with pytest.raises(serverops.ServerOpsError, match="write"):
        serverops.install_units(conn, "myapp", {"myapp-web.service": "[Unit]\n"}, {})


# =============================================================================
# install_caddyfile
# =============================================================================


def test_install_caddyfile_success(conn):
    assert serverops.install_caddyfile(
        conn, "myapp.example.com", "/etc/caddy/conf.d/myapp.caddy"
    )
    cmds = [c[0][0] for c in conn.run.call_args_list]
    assert any("systemctl reload caddy" in c for c in cmds)
    assert any("chown caddy:caddy" in c for c in cmds)


def test_install_caddyfile_restore_on_failure(conn):
    def run_side(cmd, **kw):
        if "test -f" in cmd:
            return ("", True)
        if "cat /etc/caddy" in cmd:
            return ("old config\n", True)
        if "reload caddy" in cmd:
            return ("reload error", False)
        return ("", True)

    conn.run.side_effect = run_side
    assert not serverops.install_caddyfile(conn, "new", "/etc/caddy/conf.d/myapp.caddy")
    writes = [c[0][0] for c in conn.run.call_args_list if "base64 -d" in c[0][0]]
    contents = [cmd for cmd in writes]
    # latest write restores old content
    assert "b2xkIGNvbmZpZwo=" in contents[-1]


def test_install_caddyfile_removal_on_failure_no_prior(conn):
    def run_side(cmd, **kw):
        if "test -f" in cmd:
            return ("", False)  # no prior config
        if "reload caddy" in cmd:
            return ("reload error", False)
        return ("", True)

    conn.run.side_effect = run_side
    assert not serverops.install_caddyfile(conn, "new", "/etc/caddy/conf.d/myapp.caddy")
    assert any(
        "sudo rm -f /etc/caddy/conf.d/myapp.caddy" in c[0][0]
        for c in conn.run.call_args_list
    )


# =============================================================================
# state.json + manifest reads
# =============================================================================


def test_read_state(conn):
    state = State(
        current="abc", previous=["def"], units_installed_from=None, deploys=[]
    )
    conn.run.return_value = (json.dumps(msgspec_state(state)), True)
    loaded = serverops.read_state(conn, "/opt/fujin/myapp")
    assert loaded.current == "abc"

    cat_cmd = conn.run.call_args[0][0]
    assert "/opt/fujin/myapp/store/state.json" in cat_cmd


def msgspec_state(state):
    from fujin.store import serialize_state

    import json

    return json.loads(serialize_state(state))


def test_read_state_missing(conn):
    conn.run.return_value = ("cat: no such file", False)
    assert serverops.read_state(conn, "/opt/fujin/myapp") is None


def test_write_state_atomic(conn):
    state = State(current="abc", previous=[], units_installed_from=[], deploys=[])
    serverops.write_state(conn, "/opt/fujin/myapp", state)
    cmd = conn.run.call_args[0][0]
    assert "state.json.tmp" in cmd
    assert "mv -f" in cmd


def test_write_state_failure(conn):
    conn.run.return_value = ("", False)
    state = State(current="abc", previous=[], units_installed_from=[], deploys=[])
    with pytest.raises(serverops.ServerOpsError):
        serverops.write_state(conn, "/opt/fujin/myapp", state)


def test_read_current_manifest(conn):
    manifest_data = json.dumps(
        {
            "app_name": "myapp",
            "version": "1.0.0",
            "config_fingerprint": "f" * 64,
            "dist_sha256": "d" * 64,
            "entries": {
                "systemd/myapp-web.service": {
                    "sha256": "a" * 64,
                    "mode": 0o644,
                    "kind": "unit",
                },
            },
        }
    )

    def fake_get(remote, local, verify=False):
        with open(local, "wb") as fh:
            fh.write(manifest_data.encode())

    conn.get.side_effect = fake_get
    manifest = serverops.read_current_manifest(conn, "/opt/fujin/myapp")
    assert manifest is not None
    assert "systemd/myapp-web.service" in manifest.entries
    remote = conn.get.call_args[0][0]
    assert remote == "/opt/fujin/myapp/current/manifest.json"


def test_read_current_manifest_missing(conn):
    conn.get.side_effect = FileNotFoundError("nope")
    assert serverops.read_current_manifest(conn, "/opt/fujin/myapp") is None


# =============================================================================
# enable/start & stop/disable wrappers
# =============================================================================


def test_enable_and_start(conn):
    assert serverops.enable_and_start(
        conn, ["myapp-web@1.service", "myapp-web@2.service"]
    )
    assert (
        "sudo systemctl enable --now myapp-web@1.service myapp-web@2.service"
        in conn.run.call_args[0][0]
    )


def test_enable_and_start_empty(conn):
    assert serverops.enable_and_start(conn, [])
    conn.run.assert_not_called()


def test_stop_and_disable(conn):
    assert serverops.stop_and_disable(conn, ["myapp-web@3.service"])
    cmds = [c[0][0] for c in conn.run.call_args_list]
    assert "sudo systemctl disable --now myapp-web@3.service" in cmds[0]


def test_stop_and_disable_empty(conn):
    assert serverops.stop_and_disable(conn, [])
    conn.run.assert_not_called()


def test_stop_and_disable_failure(conn):
    conn.run.return_value = ("", False)
    assert not serverops.stop_and_disable(conn, ["myapp-web@1.service"])


# =============================================================================
# Review-fix regressions
# =============================================================================


def test_install_code_python_uses_managed_python_dir(conn, tmp_path):
    dist = tmp_path / "myapp-1.0-py3-none-any.whl"
    dist.write_bytes(b"wheel")
    serverops.install_code_into_staging(
        conn, "/s", "python-package", "3.11", None, str(dist), app_name="myapp"
    )
    venv_cmd = next(c[0][0] for c in conn.run.call_args_list if "uv venv" in c[0][0])
    assert "UV_PYTHON_INSTALL_DIR=/opt/fujin/.python" in venv_cmd


def test_read_state_corrupt_json(conn):
    conn.run.return_value = ("{not json", True)
    assert serverops.read_state(conn, "/opt/fujin/myapp") is None


def test_install_caddyfile_writes_backup_before_new(conn):
    def run_side(cmd, **kw):
        if "test -f" in cmd:
            return ("", True)
        if "cat /etc/caddy" in cmd:
            return ("old config\n", True)
        if "reload caddy" in cmd:
            return ("", True)
        return ("", True)

    conn.run.side_effect = run_side
    assert serverops.install_caddyfile(conn, "new", "/etc/caddy/conf.d/myapp.caddy")
    writes = [c[0][0] for c in conn.run.call_args_list if "base64 -d" in c[0][0]]
    assert any(".fujin-bak" in c for c in writes)
    # backup written before the main config
    bak_idx = next(i for i, c in enumerate(writes) if ".fujin-bak" in c)
    main_idx = next(i for i, c in enumerate(writes) if "conf.d/myapp.caddy " in c)
    assert bak_idx < main_idx


def test_install_caddyfile_crash_recovery_restores_backup(conn):
    def run_side(cmd, **kw):
        if "test -f /etc/caddy/conf.d/myapp.caddy " in cmd + " ":
            return ("", False)  # main missing (crashed last time)
        if "test -f /etc/caddy/conf.d/myapp.caddy.fujin-bak" in cmd:
            return ("", True)  # backup present
        if "cat /etc/caddy" in cmd:
            return ("recovered config\n", True)
        if "reload caddy" in cmd:
            return ("", True)
        return ("", True)

    conn.run.side_effect = run_side
    assert serverops.install_caddyfile(conn, "new", "/etc/caddy/conf.d/myapp.caddy")
    assert any(
        "sudo cp /etc/caddy/conf.d/myapp.caddy.fujin-bak /etc/caddy/conf.d/myapp.caddy"
        in c[0][0]
        for c in conn.run.call_args_list
    )


def test_set_envfile_mode(conn):
    serverops.set_envfile_mode(conn, "/opt/fujin/myapp/releases/.staging-1")
    cmd = conn.run.call_args[0][0]
    assert "chmod 0640 /opt/fujin/myapp/releases/.staging-1/.env" in cmd
    assert "sudo" not in cmd  # setgid dir handles group ownership


def test_set_envfile_mode_failure(conn):
    conn.run.return_value = ("", False)
    with pytest.raises(serverops.ServerOpsError):
        serverops.set_envfile_mode(conn, "/opt/fujin/myapp/releases/.staging-1")


# =============================================================================
# link_unchanged (explicit dedup, replaces --link-dest)
# =============================================================================


def test_link_unchanged_parses_result(conn):
    conn.run.return_value = (
        '{"linked": 2, "failed": ["assets/skip.png"]}',
        True,
    )
    linked, failed = serverops.link_unchanged(
        conn,
        "/opt/fujin/myapp/releases/.staging-9",
        "/opt/fujin/myapp/releases/abbacaba",
        {"venv/x": 420, "assets/skip.png": 420},
    )
    assert (linked, failed) == (2, ["assets/skip.png"])
    cmd = conn.run.call_args[0][0]
    assert "os.link(src, dst)" in cmd
    assert "/opt/fujin/myapp/releases/abbacaba" in cmd
    assert "staging-9" in cmd
    assert "assets/skip.png" in cmd  # entries embedded in the script


def test_link_unchanged_empty_paths_never_runs(conn):
    assert serverops.link_unchanged(conn, "/s", "/p", {}) == (0, [])
    conn.run.assert_not_called()


def test_link_unchanged_script_failure_all_failed(conn):
    conn.run.return_value = ("boom", False)
    linked, failed = serverops.link_unchanged(conn, "/s", "/p", {"a": 420, "b": 420})
    assert (linked, failed) == (0, ["a", "b"])
