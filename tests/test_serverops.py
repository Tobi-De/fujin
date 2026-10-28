"""Tests for fujin.serverops (client-driven server operations)."""

from __future__ import annotations

import base64
import json
import shlex
import subprocess
from unittest.mock import MagicMock

import pytest
import cappa

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
# ensure_app_user
# =============================================================================


def test_ensure_app_user_single_round_trip(conn):
    """One command: group guard, user creation and the uid probe fused."""
    serverops.ensure_app_user(conn, "myapp")
    conn.run.assert_called_once()
    cmd = conn.run.call_args[0][0]
    assert cmd.startswith(
        "{ getent group myapp >/dev/null 2>&1 || sudo groupadd -f myapp; }"
    )
    assert (
        "sudo useradd --system --no-create-home --shell /usr/sbin/nologin"
        " -g myapp myapp" in cmd
    )
    # the group is ensured BEFORE the user branch (useradd -g needs it)
    assert cmd.index("groupadd -f myapp") < cmd.index("useradd")
    # and the resolved uid is the final proof the user exists
    assert cmd.endswith("&& id -u myapp")
    assert cmd.count("id -u myapp") == 2


def test_ensure_app_user_existing_user_runs_no_sudo(conn):
    """Both probes succeed, so neither ``sudo`` branch is entered."""
    conn.run.return_value = ("1000", True)
    serverops.ensure_app_user(conn, "myapp")
    cmd = conn.run.call_args[0][0]
    assert cmd.count("sudo") == 2  # both behind a ``||`` guard
    assert conn.run.call_count == 1


def test_ensure_app_user_failure_raises(conn):
    conn.run.return_value = ("", False)
    with pytest.raises(serverops.ServerOpsError, match="Failed to create app user"):
        serverops.ensure_app_user(conn, "myapp")


def _stub(bin_dir, name, body):
    path = bin_dir / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


class _LocalShellConn:
    """Run the fused command for real with stub sudo/getent/id/useradd."""

    def __init__(self, root):
        self.bin_dir = root / "bin"
        self.bin_dir.mkdir()
        self.state = root / "state"
        self.state.mkdir()
        self.log = root / "sudo.log"
        _stub(self.bin_dir, "sudo", 'printf "%s\\n" "sudo $*" >> "$LOG"\nexec "$@"\n')
        _stub(self.bin_dir, "getent", '[ -f "$STATE/group" ]\n')
        _stub(self.bin_dir, "groupadd", ': > "$STATE/group"\n')
        _stub(
            self.bin_dir,
            "useradd",
            '[ -f "$STATE/group" ] || exit 1\n: > "$STATE/user"\n',
        )
        _stub(self.bin_dir, "id", '[ -f "$STATE/user" ] || exit 1\necho 1000\n')

    def run(self, cmd, **_kwargs):
        proc = subprocess.run(
            ["bash", "-c", cmd],
            capture_output=True,
            text=True,
            env={
                "PATH": f"{self.bin_dir}:/usr/bin:/bin",
                "STATE": str(self.state),
                "LOG": str(self.log),
            },
        )
        return proc.stdout, proc.returncode == 0

    def sudo_log(self):
        return self.log.read_text() if self.log.exists() else ""


def test_ensure_app_user_repairs_missing_group_of_existing_user(tmp_path):
    """A pre-existing user whose group is gone gets the group recreated."""
    shell = _LocalShellConn(tmp_path)
    (shell.state / "user").touch()  # user exists, group missing

    serverops.ensure_app_user(shell, "myapp")

    assert "sudo groupadd -f myapp" in shell.sudo_log()
    assert "useradd" not in shell.sudo_log()


def test_ensure_app_user_creates_group_before_user(tmp_path):
    shell = _LocalShellConn(tmp_path)

    serverops.ensure_app_user(shell, "myapp")

    assert shell.sudo_log().splitlines() == [
        "sudo groupadd -f myapp",
        "sudo useradd --system --no-create-home --shell /usr/sbin/nologin"
        " -g myapp myapp",
    ]
    assert (shell.state / "user").exists()


def test_ensure_app_user_fast_path_runs_no_sudo(tmp_path):
    shell = _LocalShellConn(tmp_path)
    (shell.state / "user").touch()
    (shell.state / "group").touch()

    serverops.ensure_app_user(shell, "myapp")

    assert shell.sudo_log() == ""


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


def test_lock_holder_writes_marker_only_after_flock(conn, monkeypatch):
    """The flock is owned before the holder may write the marker."""
    monkeypatch.setattr(serverops.secrets, "token_hex", lambda _n: "TOK1")
    conn.run.side_effect = [
        ("/usr/bin/flock", True),  # flock available
        ("4242", True),  # holder spawn
        ("OURS:4242-TOK1", True),  # probe: our holder owns the flock
    ]
    token = serverops.acquire_lock(conn, "/opt/fujin/myapp", ttl=7200)
    assert token == "flock:4242"
    holder_cmd = conn.run.call_args_list[1][0][0]
    assert "flock -n 9 || exit 1" in holder_cmd
    # acquisition strictly precedes the marker write
    assert holder_cmd.index("flock -n 9") < holder_cmd.index("-TOK1")
    assert r"echo \$\$-TOK1 > /opt/fujin/myapp/store/lock.holder" in holder_cmd
    assert "sleep 7200" in holder_cmd


def test_acquire_lock_polls_instead_of_fixed_sleep(conn, monkeypatch):
    """No unconditional 0.5s sleep: the probe polls for our own token."""
    monkeypatch.setattr(serverops.secrets, "token_hex", lambda _n: "TOK1")
    conn.run.side_effect = [
        ("/usr/bin/flock", True),
        ("4242", True),
        ("OURS:4242-TOK1", True),
    ]
    serverops.acquire_lock(conn, "/opt/fujin/myapp")

    probe = conn.run.call_args_list[2][0][0]
    assert "sleep 0.5" not in probe
    assert f"sleep {serverops.LOCK_POLL_INTERVAL}" in probe
    assert f'[ "$i" -lt {serverops.LOCK_POLL_ATTEMPTS} ]' in probe
    assert "flock -n" in probe
    # success is gated on the per-call token AND the flock still being held
    assert 'case "$h" in *-"$tok")' in probe
    assert '"OURS:$h"' in probe


def test_acquire_lock_foreign_live_holder_raises(conn, monkeypatch):
    monkeypatch.setattr(serverops.secrets, "token_hex", lambda _n: "TOK1")
    conn.run.side_effect = [
        ("/usr/bin/flock", True),
        ("4242", True),  # our holder loses the race and writes nothing
        ("BUSY:999-TOK9", True),  # foreign live holder still owns the flock
    ]
    with pytest.raises(serverops.ServerOpsError, match="pid 999"):
        serverops.acquire_lock(conn, "/opt/fujin/myapp")

    commands = [c[0][0] for c in conn.run.call_args_list]
    # the foreign marker is neither removed nor overwritten; no belt switch
    assert not any("rm -f" in c for c in commands)
    assert not any("set -C" in c for c in commands)
    assert conn.run.call_count == 3


def test_acquire_lock_own_token_under_foreign_flock_never_wins(conn, monkeypatch):
    """P1 regression: a marker carrying our token while a foreign process
    holds the flock must never be accepted as success."""
    monkeypatch.setattr(serverops.secrets, "token_hex", lambda _n: "TOK1")
    conn.run.side_effect = [
        ("/usr/bin/flock", True),
        ("4242", True),
        ("BUSY:4242-TOK1", True),  # our token, but the flock is still taken
    ]
    with pytest.raises(serverops.ServerOpsError, match="Another deployment"):
        serverops.acquire_lock(conn, "/opt/fujin/myapp")


def test_acquire_lock_live_holder_with_free_flock_raises(conn, monkeypatch):
    monkeypatch.setattr(serverops.secrets, "token_hex", lambda _n: "TOK1")
    conn.run.side_effect = [
        ("/usr/bin/flock", True),
        ("4242", True),
        ("ALIVE:999-TOK9", True),  # holder alive but the flock is free
    ]
    with pytest.raises(serverops.ServerOpsError, match="pid 999"):
        serverops.acquire_lock(conn, "/opt/fujin/myapp")
    assert not any("rm -f" in c[0][0] for c in conn.run.call_args_list)


def test_acquire_lock_stale_holder_is_stolen(conn, monkeypatch):
    monkeypatch.setattr(serverops.secrets, "token_hex", lambda _n: "TOK1")
    conn.run.side_effect = [
        ("/usr/bin/flock", True),
        ("4242", True),  # holder spawn
        ("DEAD:999-TOK9", True),  # stale marker, flock free -> steal
        ("", True),  # cleanup kill
        ("", True),  # compare-and-delete of the stale marker
        ("5555", True),  # retried holder spawn
        ("OURS:222-TOK1", True),  # acquired on retry
    ]
    token = serverops.acquire_lock(conn, "/opt/fujin/myapp")
    assert token == "flock:222"
    delete = conn.run.call_args_list[4][0][0]
    assert "rm -f /opt/fujin/myapp/store/lock.holder" in delete
    # only the marker we observed may be removed
    assert "999-TOK9" in delete


def test_acquire_lock_flock_free_falls_back(conn, monkeypatch):
    monkeypatch.setattr(serverops.secrets, "token_hex", lambda _n: "TOK1")
    conn.run.side_effect = [
        ("/usr/bin/flock", True),
        ("4242", True),  # holder spawn
        ("FREE:", True),  # our holder never acquired: no marker, flock free
        ("", True),  # kill holder
        ("", True),  # no marker to delete
        ("", True),  # noclobber
    ]
    token = serverops.acquire_lock(conn, "/opt/fujin/myapp")
    assert token == "file:/opt/fujin/myapp/.deploy_lock"


def test_acquire_lock_stale_persists_raises(conn, monkeypatch):
    monkeypatch.setattr(serverops.secrets, "token_hex", lambda _n: "TOK1")
    conn.run.side_effect = [
        ("/usr/bin/flock", True),
        ("4242", True),
        ("DEAD:999-TOK9", True),
        ("", True),
        ("", True),
        ("5555", True),
        ("DEAD:999-TOK9", True),  # stale again on retry
        ("", True),
        ("", True),
    ]
    with pytest.raises(serverops.ServerOpsError, match="stale holder"):
        serverops.acquire_lock(conn, "/opt/fujin/myapp")


def test_parse_holder_pid():
    assert serverops.parse_holder_pid("OURS:42-abc") == "42"
    assert serverops.parse_holder_pid("BUSY:?") is None
    assert serverops.parse_holder_pid("FREE:") is None


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
    # dist and requirements are part of the rsynced staging tree: nothing is
    # uploaded again (the pip command only asserts requirements.txt exists)
    assert conn.put.call_args_list == []

    cmds = [c[0][0] for c in conn.run.call_args_list]
    assert any(
        "uv venv --relocatable -p 3.11 --managed-python" in c and "staging-1/venv" in c
        for c in cmds
    )
    pip = next(c for c in cmds if "uv pip install --python" in c)
    assert "--no-deps" in pip
    assert "staging-1/bin/myapp-1.0-py3-none-any.whl" in pip
    assert "test -f /opt/fujin/myapp/releases/.staging-1/requirements.txt" in pip
    assert "-r /opt/fujin/myapp/releases/.staging-1/requirements.txt" in pip
    assert "UV_COMPILE_BYTECODE=1" in pip
    assert "UV_PYTHON_INSTALL_DIR=/opt/fujin/.python" in pip
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


def test_install_code_python_missing_requirements_fails(conn):
    conn.run.side_effect = [
        ("", True),  # uv venv
        ("fujin: /s/requirements.txt missing from the staging tree", False),
    ]
    with pytest.raises(serverops.ServerOpsError, match="missing from the staging"):
        serverops.install_code_into_staging(
            conn,
            "/s",
            "python-package",
            "3.11",
            "local/requirements.txt",
            "bin/myapp-1.0-py3-none-any.whl",
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


def test_install_units_creates_dropin_directories(conn):
    serverops.install_units(
        conn,
        "myapp",
        {"myapp-web.service": "[Service]\n"},
        {"myapp-web.service": {"limits.conf": "[Service]\nLimitNOFILE=8192\n"}},
    )
    cmds = [c[0][0] for c in conn.run.call_args_list]
    mkdir_index = cmds.index("sudo mkdir -p /etc/systemd/system/myapp-web.service.d")
    write_index = next(
        i
        for i, cmd in enumerate(cmds)
        if "/etc/systemd/system/myapp-web.service.d/limits.conf" in cmd
    )
    # The directory must exist before the first write into it.
    assert mkdir_index < write_index


def test_install_units_dropin_directory_failure(conn):
    conn.run.side_effect = lambda cmd, **kw: (
        ("", True)
        if cmd != "sudo mkdir -p /etc/systemd/system/myapp-web.service.d"
        else ("", False)
    )
    with pytest.raises(serverops.ServerOpsError, match="myapp-web.service.d"):
        serverops.install_units(
            conn,
            "myapp",
            {},
            {"myapp-web.service": {"limits.conf": "[Service]\n"}},
        )


def test_install_units_removes_empty_declared_dropin_dir(conn):
    def run(cmd, **kw):
        if cmd.startswith("cd /etc/systemd/system && find . -maxdepth 2"):
            return "./myapp-web.service.d\n./myapp-web.service.d/old.conf\n", True
        if cmd.startswith("cd /etc/systemd/system && find . -maxdepth 1"):
            return "./myapp-web.service.d\n", True
        return "", True

    conn.run.side_effect = run
    serverops.install_units(conn, "myapp", {"myapp-web.service": "[Unit]\n"}, {})
    cmds = [c[0][0] for c in conn.run.call_args_list]
    assert "sudo rm -rf /etc/systemd/system/myapp-web.service.d" in cmds


def test_install_units_keeps_non_empty_dropin_dir(conn):
    def run(cmd, **kw):
        if cmd.startswith("cd /etc/systemd/system && find . -maxdepth 2"):
            return "./myapp-web.service.d\n./myapp-web.service.d/keep.conf\n", True
        return "", True  # the -empty probe finds nothing

    conn.run.side_effect = run
    serverops.install_units(conn, "myapp", {"myapp-web.service": "[Unit]\n"}, {})
    cmds = [c[0][0] for c in conn.run.call_args_list]
    removed = [
        target
        for cmd in cmds
        if cmd.startswith("sudo rm -rf ")
        for target in shlex.split(cmd)[3:]
    ]
    assert "/etc/systemd/system/myapp-web.service.d" not in removed
    assert "/etc/systemd/system/myapp-web.service.d/keep.conf" in removed


def test_install_units_stale_cleanup(conn):
    conn.run.side_effect = lambda cmd, **kw: (
        (
            "./myapp-gone@.service.d\n"
            "./myapp-old.service\n"
            "./myapp-web@.service\n"
            "./myapp-web@.service.d/old.conf\n",
            True,
        )
        if cmd.startswith("cd /etc/systemd/system && find .")
        else ("", True)
    )
    serverops.install_units(conn, "myapp", {"myapp-web@.service": "[Unit]\n"}, {})
    find_cmd = conn.run.call_args_list[0][0][0]
    # Depth-2 listing: top-level units/dirs plus files inside kept ``*.d/``
    # dirs. The globs are quoted so the remote shell leaves them to find(1).
    assert find_cmd == (
        "cd /etc/systemd/system && find . -maxdepth 2 \\( "
        "-name 'myapp*' -o -path './myapp*.d/*' \\) -print 2>/dev/null | sort"
    )
    cmds = [c[0][0] for c in conn.run.call_args_list]
    assert any("disable --now --quiet" in c and "myapp-old.service" in c for c in cmds)
    reset = next(c for c in cmds if "reset-failed" in c)
    assert "myapp-old.service" in reset
    assert "myapp-gone@.service.d" in reset
    # depth-2 dropin file is stale but never fed to systemctl
    assert "old.conf" not in reset
    rm = next(c for c in cmds if "rm -rf" in c)
    assert "/etc/systemd/system/myapp-old.service" in rm
    assert "/etc/systemd/system/myapp-gone@.service.d" in rm
    assert "/etc/systemd/system/myapp-web@.service.d/old.conf" in rm
    assert "/etc/systemd/system/myapp-web@.service" not in rm.split()


def test_install_units_no_stale_units(conn):
    conn.run.side_effect = lambda cmd, **kw: ("", True)  # find matches nothing
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
# template_instance_plan
# =============================================================================


def test_template_instance_plan_sees_stopped_enabled_instance(conn):
    """``list-unit-files`` never reports instances; ``.wants`` symlinks do."""

    def run(cmd, **kw):
        if "list-unit-files" in cmd:
            return "", True  # real systemd: only the bare template is listed
        if cmd.startswith("find /etc/systemd/system"):
            return "myapp-web@1.service\nmyapp-web@2.service\n", True
        if "list-units" in cmd:
            return "myapp-web@1.service loaded active running\n", True
        return "", True

    conn.run.side_effect = run
    start, stop = serverops.template_instance_plan(
        conn, "myapp", ["myapp-web@1", "myapp-web@2"]
    )
    assert start == ["myapp-web@2.service"]
    assert stop == []


def test_template_instance_plan_stops_extra_enabled_instance(conn):
    def run(cmd, **kw):
        if "list-unit-files" in cmd:
            return "", True
        if cmd.startswith("find /etc/systemd/system"):
            return "myapp-web@1.service\nmyapp-web@3.service\n", True
        if "list-units" in cmd:
            return "myapp-web@1.service loaded active running\n", True
        return "", True

    conn.run.side_effect = run
    start, stop = serverops.template_instance_plan(conn, "myapp", ["myapp-web@1"])
    assert start == []
    assert stop == ["myapp-web@3.service"]


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


# =============================================================================
# finalize_deploy / collect_garbage
# =============================================================================


def _finalize_state(**kwargs) -> State:
    return State(**{"previous": [], "deploys": [], **kwargs})


def test_finalize_deploy_fuses_state_releases_and_audit(conn):
    current, prev, old1, old2 = "a" * 64, "b" * 64, "c" * 64, "d" * 64
    state = _finalize_state(current=current, previous=[prev])
    conn.run.side_effect = [
        (
            "FUJIN-RELEASES\n"
            f"{current}\n{prev}\n{old1}\n.staging-99\n{old2}\n"
            "FUJIN-RELEASES-END\n",
            True,
        ),
        ("", True),
    ]
    output = MagicMock()

    releases, removed = serverops.finalize_deploy(
        conn,
        "/opt/fujin/myapp",
        state,
        keep_budget=0,
        audit_line='{"operation": "deploy"}',
        app_name="myapp",
        output=output,
    )

    assert releases == [current, prev, old1, old2]
    assert removed == (old1, old2)

    script = conn.run.call_args_list[0][0][0]
    # state write, release listing and the audit append in ONE round-trip
    assert "state.json.tmp" in script and "mv -f" in script
    assert "ls -1t releases/" in script
    assert "sudo mkdir -p /opt/fujin/.audit" in script
    assert "tee -a /opt/fujin/.audit/myapp.log" in script
    assert '{\\"operation\\": \\"deploy\\"}' in script

    # every GC digest goes out in a single rm -rf
    assert conn.run.call_count == 2
    rm_cmd = conn.run.call_args_list[1][0][0]
    assert rm_cmd == (
        f"rm -rf /opt/fujin/myapp/releases/{old1} /opt/fujin/myapp/releases/{old2}"
    )
    output.info.assert_called_once()


def test_finalize_deploy_names_the_failing_step(conn):
    state = _finalize_state(current="a" * 64)
    conn.run.return_value = ("FUJIN-FAIL:state", False)
    with pytest.raises(serverops.ServerOpsError, match="finalize step: state"):
        serverops.finalize_deploy(
            conn, "/opt/fujin/myapp", state, keep_budget=0, output=MagicMock()
        )


def test_finalize_deploy_names_the_failing_gc_step(conn):
    current, old = "a" * 64, "c" * 64
    state = _finalize_state(current=current)
    conn.run.side_effect = [
        (f"FUJIN-RELEASES\n{current}\n{old}\nFUJIN-RELEASES-END\n", True),
        ("rm: permission denied", False),
    ]
    output = MagicMock()
    _releases, removed = serverops.finalize_deploy(
        conn, "/opt/fujin/myapp", state, keep_budget=0, output=output
    )
    assert removed == (old,)
    message = output.warning.call_args[0][0]
    assert "finalize step: garbage collection" in message


def test_finalize_deploy_audit_failure_is_not_fatal(conn):
    state = _finalize_state(current="a" * 64)
    conn.run.return_value = (
        "FUJIN-RELEASES\naaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
        "FUJIN-RELEASES-END\nFUJIN-WARN:audit\n",
        True,
    )
    output = MagicMock()
    releases, removed = serverops.finalize_deploy(
        conn,
        "/opt/fujin/myapp",
        state,
        keep_budget=0,
        audit_line="{}",
        app_name="myapp",
        output=output,
    )
    assert releases == ["a" * 64]
    assert removed == ()
    assert "audit entry" in output.warning.call_args[0][0]


def test_finalize_deploy_without_state_skips_write_and_gc(conn):
    """Auto-rollback with no recorded state: audit only, no state/GC."""
    conn.run.return_value = ("FUJIN-RELEASES\nx\nFUJIN-RELEASES-END\n", True)
    releases, removed = serverops.finalize_deploy(
        conn,
        "/opt/fujin/myapp",
        None,
        keep_budget=0,
        audit_line="{}",
        app_name="myapp",
        output=MagicMock(),
    )
    assert releases == ["x"]
    assert removed == ()
    script = conn.run.call_args_list[0][0][0]
    assert "state.json.tmp" not in script
    assert conn.run.call_count == 1


def test_finalize_deploy_without_current_skips_gc(conn):
    """No live release anchors the keep-set: deleting would hit every release."""
    conn.run.return_value = (
        "FUJIN-RELEASES\n" + "a" * 64 + "\n" + "b" * 64 + "\nFUJIN-RELEASES-END\n",
        True,
    )
    output = MagicMock()
    releases, removed = serverops.finalize_deploy(
        conn,
        "/opt/fujin/myapp",
        _finalize_state(current=None),
        keep_budget=0,
        output=output,
    )
    assert releases == ["a" * 64, "b" * 64]
    assert removed == ()
    assert conn.run.call_count == 1  # no rm -rf round-trip
    output.info.assert_not_called()


def test_collect_garbage_one_command(conn):
    current, old1, old2 = "a" * 64, "b" * 64, "c" * 64
    state = _finalize_state(current=current)
    output = MagicMock()

    killed = serverops.collect_garbage(
        conn, "/opt/fujin/myapp", state, [current, old1, old2], 0, output
    )

    assert killed == (old1, old2)
    conn.run.assert_called_once()
    assert conn.run.call_args[0][0] == (
        f"rm -rf /opt/fujin/myapp/releases/{old1} /opt/fujin/myapp/releases/{old2}"
    )
    output.info.assert_called_once()


def test_write_state_failure_semantics_unchanged(conn):
    """write_state itself is still a single atomic command (rollback/doctor)."""
    state = _finalize_state(current="a" * 64)
    serverops.write_state(conn, "/opt/fujin/myapp", state)
    cmd = conn.run.call_args[0][0]
    assert "base64 -d" in cmd and "mv -f" in cmd


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


# =============================================================================
# run_health_probe (#88)
# =============================================================================


def test_run_health_probe_success(conn):
    conn.run.return_value = ("", True)

    ok, output = serverops.run_health_probe(conn, "curl -sf http://x/health")

    assert (ok, output) == (True, "")
    cmd, kwargs = conn.run.call_args[0][0], conn.run.call_args[1]
    assert cmd == "curl -sf http://x/health"
    # non-fatal + quiet: a failing probe must not dump its output
    assert kwargs == {"warn": True, "hide": True}


def test_run_health_probe_is_single_shot_and_reports_output(conn):
    conn.run.return_value = ("curl: (22) 500", False)

    ok, output = serverops.run_health_probe(conn, "probe")

    assert ok is False
    assert output == "curl: (22) 500"
    # No retry policy in the I/O layer: exactly one call.
    assert conn.run.call_count == 1


def test_run_health_probe_strips_output(conn):
    conn.run.return_value = ("  trailing newline\n", True)

    assert serverops.run_health_probe(conn, "probe") == (True, "trailing newline")


# =============================================================================
# read_remote_file
# =============================================================================


def test_read_remote_file_missing_file_returns_none(conn, tmp_path):
    """A missing remote file (e.g. a first deploy's live manifest) is absent,
    not fatal: the download raises ``UploadError`` (a ``cappa.Exit``, i.e. a
    ``SystemExit``), which must not escape as an exception."""
    from fujin.errors import UploadError

    conn.get.side_effect = UploadError("Download failed for manifest.json")

    assert (
        serverops.read_remote_file(conn, "/opt/fujin/app/current/manifest.json") is None
    )


def test_read_remote_file_fatal_message(conn):
    from fujin.errors import UploadError

    conn.get.side_effect = UploadError("nope")

    with pytest.raises(cappa.Exit) as excinfo:
        serverops.read_remote_file(conn, "/missing", fatal="Cannot read unit")

    assert excinfo.value.code == 1
    assert excinfo.value.message == "Cannot read unit"
