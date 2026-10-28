"""Tests for ``fujin doctor`` (read-only checks + safe repairs).

A scripted fake connection answers every probe the command makes, so the
tests can assert both the reported checks and the exact repair commands
without a live host.
"""

from __future__ import annotations

import base64
import fnmatch
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import cappa
import msgspec

from fujin import serverops, store
from fujin.commands.doctor import FAIL, OK, WARN, Doctor
from fujin.config import Config, InstallationMode
from fujin.manifest import build_manifest, manifest_to_dict
from fujin.store import DeployRecord, State

APP = "testapp"
APP_DIR = f"/opt/fujin/{APP}"
UNIT_NAME = "testapp-web@.service"
INSTANCE = "testapp-web@1.service"
UNIT_CONTENT = "[Service]\nType=simple\nExecStart=/bin/true\n"

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
DIGEST_ORPHAN = "c" * 64


def make_config(minimal_config_dict, replicas=None):
    data = dict(minimal_config_dict)
    data["replicas"] = replicas or {"web": 1}
    return msgspec.convert(data, type=Config)


def make_manifest(unit_files=None):
    files = (
        unit_files if unit_files is not None else {f"systemd/{UNIT_NAME}": UNIT_CONTENT}
    )
    return build_manifest(
        app_name=APP,
        version="1.0.0",
        config_fingerprint="fp",
        dist_sha256="dist",
        files=files,
    )


class FakeConn:
    """Scripted connection for the doctor probes."""

    def __init__(
        self,
        *,
        link=f"releases/{DIGEST_A}",
        state=None,
        releases=(DIGEST_A,),
        installed=None,
        release_units=None,
        manifest=None,
        staging=(),
        lock="free:",
        modes=None,
        caddy=True,
        caddy_config=True,
        validate_ok=True,
        caddy_groups=(APP,),
        enabled=(INSTANCE,),
        active=(INSTANCE,),
        audit=(),
        uv_relocatable=True,
    ):
        self.link = link
        self.state = state
        self.releases = list(releases)
        self.installed = (
            installed if installed is not None else {UNIT_NAME: UNIT_CONTENT}
        )
        self.release_units = (
            release_units if release_units is not None else {UNIT_NAME: UNIT_CONTENT}
        )
        self.manifest = manifest if manifest is not None else make_manifest()
        self.staging = list(staging)
        self.lock = lock
        self.modes = modes or {
            APP_DIR: (0o2755, "deploy", APP),
            f"{APP_DIR}/current/.env": (0o640, "deploy", APP),
            f"/run/{APP}": (0o750, APP, APP),
        }
        self.caddy = caddy
        self.caddy_config = caddy_config
        self.validate_ok = validate_ok
        self.caddy_groups = caddy_groups
        self.enabled = list(enabled)
        self.active = list(active)
        self.audit = list(audit)
        self.uv_relocatable = uv_relocatable
        self.calls: list[str] = []
        self.gets: list[tuple[str, str]] = []

    # -- SCP -----------------------------------------------------------
    def get(self, remote, local, verify=False):
        self.gets.append((remote, local))
        if remote.endswith("/manifest.json"):
            data = json.dumps(manifest_to_dict(self.manifest))
        elif "/systemd/" in remote:
            rel = remote.split("/systemd/", 1)[1]
            if rel not in self.release_units:
                raise FileNotFoundError(remote)
            data = self.release_units[rel]
        else:
            raise FileNotFoundError(remote)
        with open(local, "w") as fh:
            fh.write(data)

    # -- installed-tree helpers ----------------------------------------
    def _find_installed(self, cmd):
        """Model the depth-2 ``find`` over /etc/systemd/system.

        Directories are implicit in ``installed`` (flat path -> content), so
        parent dirs of dropin files are added to the listing the way the real
        filesystem would show them.
        """
        name_match = re.search(r"-name '([^']+)'", cmd)
        assert name_match, cmd
        pattern = name_match.group(1)
        path_match = re.search(r"-path '([^']+)'", cmd)
        path_pattern = path_match.group(1) if path_match else None

        entries: set[str] = set()
        for key in self.installed:
            entries.add(key)
            if "/" in key:
                entries.add(key.split("/", 1)[0])

        lines = []
        for rel in sorted(entries):
            if rel.count("/") + 1 > 2:  # find -maxdepth 2
                continue
            full = f"./{rel}"
            if fnmatch.fnmatchcase(rel.rsplit("/", 1)[-1], pattern) or (
                path_pattern and fnmatch.fnmatchcase(full, path_pattern)
            ):
                lines.append(full)
        return "\n".join(lines) + ("\n" if lines else ""), True

    # -- fused install script ------------------------------------------
    def _run_install_script(self, cmd):
        """Execute the fused ``install_units`` script against a scratch tree.

        The real script runs locally with stub ``sudo``/``systemctl``; the
        resulting file tree is read back into ``self.installed``. That keeps
        the fake honest about stale removal, dropin pruning and daemon-reload
        ordering instead of re-implementing the script in Python.
        """
        workdir = Path(tempfile.mkdtemp())
        root = workdir / "systemd"
        root.mkdir()
        for rel, content in self.installed.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content or "")
        bindir = workdir / "bin"
        bindir.mkdir()
        sudo = bindir / "sudo"
        sudo.write_text('#!/bin/sh\nexec "$@"\n')
        sudo.chmod(0o755)
        systemctl = bindir / "systemctl"
        systemctl.write_text("#!/bin/sh\nexit 0\n")
        systemctl.chmod(0o755)
        env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}")
        result = subprocess.run(
            ["sh", "-c", cmd.replace("/etc/systemd/system", str(root))],
            capture_output=True,
            text=True,
            env=env,
        )
        self.installed = {
            str(path.relative_to(root)): path.read_text()
            for path in root.rglob("*")
            if path.is_file()
        }
        return result.stdout, result.returncode == 0

    # -- exec ----------------------------------------------------------
    def run(self, cmd, **kw):
        self.calls.append(cmd)

        if serverops.INSTALL_INSTALLED_END in cmd:
            return self._run_install_script(cmd)

        if cmd.startswith("readlink "):
            return (self.link or "", self.link is not None)

        if cmd.startswith("stat -c"):
            path = cmd.split()[-1]
            info = self.modes.get(path)
            if info is None:
                return "", False
            mode, owner, group = info
            return f"{mode:o} {owner} {group}", True

        if cmd.startswith("printf '%s'") and "state.json.tmp" in cmd:
            return "", True

        if cmd.startswith("cat ") and "/store/state.json" in cmd:
            return (self.state or "", self.state is not None)

        if "ls -1t releases/" in cmd:
            return "\n".join(self.releases) + "\n", True

        if cmd.startswith("find ") and ".staging-*" in cmd:
            return "\n".join(self.staging) + ("\n" if self.staging else ""), True

        if cmd.startswith("cd /etc/systemd/system"):
            return self._find_installed(cmd)

        if cmd.startswith("ls -1 /etc/systemd/system/"):
            return (
                "\n".join(f"/etc/systemd/system/{rel}" for rel in self.installed),
                True,
            )

        if cmd.startswith("cat ") and "/etc/systemd/system/" in cmd:
            rel = cmd.split("/etc/systemd/system/", 1)[1].strip("'\"")
            content = self.installed.get(rel)
            return (content or "", content is not None)

        if "command -v caddy" in cmd:
            return ("/usr/bin/caddy", True) if self.caddy else ("", False)

        if cmd.startswith("uv venv --help"):
            flags = (
                "--relocatable  Create a relocatable virtual environment"
                if (self.uv_relocatable)
                else "--python  Interpreters to use"
            )
            return f"Usage: uv venv [OPTIONS] [PATH]\n\nOptions:\n  {flags}\n", True

        if "caddy validate" in cmd:
            return (
                ("Valid configuration\n", self.validate_ok)
                if self.validate_ok
                else (
                    "Error: adapting config: invalid\n",
                    False,
                )
            )

        if cmd.startswith("id -nG caddy"):
            if self.caddy_groups is None:
                return "", False
            return " ".join(self.caddy_groups), True

        if cmd.startswith("test -f ") and ".caddy" in cmd:
            return "", self.caddy_config

        if cmd.startswith("test -f ") and "/.audit/" in cmd:
            return "", bool(self.audit)

        if cmd.startswith("tail ") or cmd.startswith("cat ") and "/.audit/" in cmd:
            return "\n".join(json.dumps(record) for record in self.audit), True

        if "list-unit-files" in cmd:
            # Fused plan probe: enabled listing, ``.wants`` links, active
            # listing, separated by end-of-section markers.
            return (
                "".join(f"{name} enabled\n" for name in self.enabled)
                + f"{serverops.PLAN_ENABLED_END}\n"
                + f"{serverops.PLAN_LINKS_END}\n"
                + "".join(f"{name} loaded active running\n" for name in self.active)
            ), True

        if "state=free; pid=" in cmd:
            return self.lock + "\n", True

        return "", True


def state_bytes(state):
    return store.serialize_state(state).decode()


def run_doctor(monkeypatch, config, conn, *, fix=False):
    """Run Doctor against FakeConn, returning (checks_messages, exit)."""
    output = MagicMock()
    with (
        patch("fujin.config.Config.read", return_value=config),
        patch("fujin.connection.connection") as mock_connection,
        patch.object(Doctor, "output", output),
    ):
        mock_connection.return_value.__enter__.return_value = conn
        mock_connection.return_value.__exit__.return_value = None
        doctor = Doctor(fix=fix)
        try:
            doctor()
            exit_code = 0
        except cappa.Exit as exc:
            exit_code = exc.code
    messages = [call.args[0] for call in output.output.call_args_list]
    return messages, exit_code


def statuses(messages):
    result = []
    for message in messages:
        for status, color in ((OK, "green"), (WARN, "yellow"), (FAIL, "red")):
            if f"[{color}]" in message:
                result.append(status)
                break
    return result


MUTATING_MARKERS = (
    "rm -",
    "rm -rf",
    "chmod",
    "systemctl start",
    "systemctl stop",
    "systemctl enable",
    "systemctl disable",
    "systemctl reset-failed",
    "systemctl daemon-reload",
    "tee ",
    "mv -f",
    "sudo caddy reload",
)


def mutating(calls):
    return [cmd for cmd in calls if any(marker in cmd for marker in MUTATING_MARKERS)]


def clean_state(current=DIGEST_A):
    return State(
        current=current,
        previous=[],
        units_installed_from=current,
        deploys=[
            DeployRecord(manifest=current, at="2026-01-01T00:00:00+00:00", result="ok")
        ],
    )


def clean_conn(**overrides):
    options = {
        "state": state_bytes(clean_state()),
        "releases": (DIGEST_A,),
        "lock": "free:",
        "audit": [
            {
                "timestamp": "2026-01-01T00:00:00+00:00",
                "operation": "deploy",
                "digest": DIGEST_A,
            }
        ],
    }
    options.update(overrides)
    return FakeConn(**options)


# ============================================================================
# Clean deploy
# ============================================================================


def test_clean_deploy_reports_all_ok_and_exits_zero(minimal_config_dict, monkeypatch):
    conn = clean_conn()
    messages, exit_code = run_doctor(
        monkeypatch, make_config(minimal_config_dict), conn
    )

    assert exit_code == 0
    assert messages, "doctor must print its checks"
    assert set(statuses(messages)) == {OK}
    assert mutating(conn.calls) == []


# ============================================================================
# state.json vs current symlink + replica convergence
# ============================================================================


def broken_state_and_instance_conn(**overrides):
    options = {
        "link": f"releases/{DIGEST_A}",
        "state": state_bytes(
            State(
                current=DIGEST_B,
                previous=[],
                units_installed_from=DIGEST_B,
                deploys=[
                    DeployRecord(
                        manifest=DIGEST_B, at="2026-01-01T00:00:00+00:00", result="ok"
                    )
                ],
            )
        ),
        "releases": (DIGEST_A, DIGEST_B),
        "enabled": (),
        "active": (),
    }
    options.update(overrides)
    return FakeConn(**options)


def test_broken_symlink_and_stopped_instance_reported(minimal_config_dict, monkeypatch):
    conn = broken_state_and_instance_conn()
    messages, exit_code = run_doctor(
        monkeypatch, make_config(minimal_config_dict), conn
    )

    assert exit_code == 1
    text = "\n".join(messages)
    assert "state.json current" in text and DIGEST_B[:12] in text
    assert "start: testapp-web@1.service" in text
    assert FAIL in statuses(messages)
    assert mutating(conn.calls) == []


def test_fix_repairs_state_and_instances(minimal_config_dict, monkeypatch):
    conn = broken_state_and_instance_conn()
    messages, exit_code = run_doctor(
        monkeypatch, make_config(minimal_config_dict), conn, fix=True
    )

    assert exit_code == 0
    assert OK in statuses(messages)

    expected_state = State(
        current=DIGEST_A,
        previous=[DIGEST_B],
        units_installed_from=DIGEST_A,
        deploys=[
            DeployRecord(manifest=DIGEST_B, at="2026-01-01T00:00:00+00:00", result="ok")
        ],
    )
    encoded = base64.b64encode(store.serialize_state(expected_state)).decode()
    expected_write = (
        f"printf '%s' {encoded} | base64 -d > {APP_DIR}/store/state.json.tmp"
        f" && mv -f {APP_DIR}/store/state.json.tmp {APP_DIR}/store/state.json"
    )
    assert expected_write in conn.calls
    assert "sudo systemctl enable --now testapp-web@1.service" in conn.calls


# ============================================================================
# Orphaned releases + stale staging dirs
# ============================================================================


def orphan_conn(**overrides):
    options = {
        "state": state_bytes(clean_state()),
        "releases": (DIGEST_A, DIGEST_ORPHAN),
        "staging": (f"{APP_DIR}/releases/.staging-999",),
    }
    options.update(overrides)
    return FakeConn(**options)


def test_orphans_reported_without_mutation(minimal_config_dict, monkeypatch):
    conn = orphan_conn()
    messages, exit_code = run_doctor(
        monkeypatch, make_config(minimal_config_dict), conn
    )

    assert exit_code == 1
    text = "\n".join(messages)
    assert DIGEST_ORPHAN[:12] in text
    assert ".staging-999" in text
    assert mutating(conn.calls) == []


def test_fix_removes_orphans_and_staging(minimal_config_dict, monkeypatch):
    conn = orphan_conn()
    messages, exit_code = run_doctor(
        monkeypatch, make_config(minimal_config_dict), conn, fix=True
    )

    assert exit_code == 0
    assert f"rm -rf {APP_DIR}/releases/{DIGEST_ORPHAN}" in conn.calls
    assert f"rm -rf {APP_DIR}/releases/.staging-999" in conn.calls
    assert f"rm -rf {APP_DIR}/releases/{DIGEST_A}" not in conn.calls


def test_unreadable_state_never_removes_releases(minimal_config_dict, monkeypatch):
    """A corrupt state.json cannot identify the rollback target: delete nothing."""
    conn = FakeConn(
        state=None,
        link=f"releases/{DIGEST_A}",
        releases=(DIGEST_A, DIGEST_ORPHAN),
        lock="free:",
    )
    messages, exit_code = run_doctor(
        monkeypatch, make_config(minimal_config_dict), conn, fix=True
    )

    assert exit_code == 1
    assert "state.json was unreadable; removal skipped" in "\n".join(messages)
    assert not [cmd for cmd in conn.calls if "releases/" in cmd and "rm -rf" in cmd]


# ============================================================================
# Deploy lock guard
# ============================================================================


def test_held_lock_skips_all_repairs(minimal_config_dict, monkeypatch):
    """While a deploy may be running, --fix must not touch state/releases."""
    conn = broken_state_and_instance_conn(
        lock="held:",
        staging=(f"{APP_DIR}/releases/.staging-999",),
    )
    messages, exit_code = run_doctor(
        monkeypatch, make_config(minimal_config_dict), conn, fix=True
    )

    text = "\n".join(messages)
    assert exit_code == 1
    assert "deploy lock present; fixes skipped" in text
    assert "left untouched" in text
    assert mutating(conn.calls) == []
    assert f"rm -rf {APP_DIR}/releases/.staging-999" not in conn.calls


# ============================================================================
# Drifted dropins / unit-less releases
# ============================================================================


DROPIN = "10-extra.conf"
DROPIN_CONTENT = "[Service]\nNice=5\n"


def dropin_release(**overrides):
    release_units = {
        UNIT_NAME: UNIT_CONTENT,
        f"{UNIT_NAME}.d/{DROPIN}": DROPIN_CONTENT,
    }
    options = {
        "manifest": make_manifest(
            {
                f"systemd/{UNIT_NAME}": UNIT_CONTENT,
                f"systemd/{UNIT_NAME}.d/{DROPIN}": DROPIN_CONTENT,
            }
        ),
        "release_units": release_units,
        "installed": {
            **release_units,
            f"{UNIT_NAME}.d/stray.conf": "[Service]\nUser=root\n",
        },
    }
    options.update(overrides)
    return FakeConn(**options)


def test_stray_dropin_inside_kept_dir_is_removed(minimal_config_dict, monkeypatch):
    conn = dropin_release()
    messages, exit_code = run_doctor(
        monkeypatch, make_config(minimal_config_dict), conn, fix=True
    )

    assert exit_code == 0
    script = "\n".join(conn.calls)
    # stale dropin files are removed by the fused install script, but the
    # kept dropin directory (still declared) survives
    assert f"{UNIT_NAME}.d/stray.conf" in script
    assert f'/etc/systemd/system/{UNIT_NAME}.d"' not in script
    assert f"{UNIT_NAME}.d/stray.conf" not in conn.installed


def test_unit_less_release_reports_and_removes_stale_units(
    minimal_config_dict, monkeypatch
):
    stray = "testapp-web@.service"
    conn = FakeConn(
        manifest=make_manifest({}),
        release_units={},
        installed={stray: UNIT_CONTENT},
        enabled=(),
        active=(),
    )
    messages, exit_code = run_doctor(
        monkeypatch, make_config(minimal_config_dict), conn
    )
    assert exit_code == 1
    assert f"stray: {stray}" in "\n".join(messages)

    conn = FakeConn(
        manifest=make_manifest({}),
        release_units={},
        installed={stray: UNIT_CONTENT},
        enabled=(),
        active=(),
    )
    messages, exit_code = run_doctor(
        monkeypatch, make_config(minimal_config_dict), conn, fix=True
    )
    assert exit_code == 0
    assert stray not in conn.installed
    assert "sudo rm -rf" in "\n".join(conn.calls)


# ============================================================================
# Missing current symlink
# ============================================================================


def test_missing_symlink_reported_and_relinked(minimal_config_dict, monkeypatch):
    conn = clean_conn(link=None, state=state_bytes(clean_state(DIGEST_A)))
    messages, exit_code = run_doctor(
        monkeypatch, make_config(minimal_config_dict), conn
    )
    assert exit_code == 1
    assert "re-run with --fix to relink it" in "\n".join(messages)
    assert not [cmd for cmd in conn.calls if "ln -sfn" in cmd]

    conn = clean_conn(link=None, state=state_bytes(clean_state(DIGEST_A)))
    messages, exit_code = run_doctor(
        monkeypatch, make_config(minimal_config_dict), conn, fix=True
    )
    assert exit_code == 0
    assert any(
        cmd.startswith(f"ln -sfn releases/{DIGEST_A} ") and "mv -T " in cmd
        for cmd in conn.calls
    )


# ============================================================================
# Deploy lock
# ============================================================================


def test_live_lock_is_never_removed(minimal_config_dict, monkeypatch):
    conn = clean_conn(
        lock="live:4242",
        staging=(f"{APP_DIR}/releases/.staging-999",),
    )
    messages, exit_code = run_doctor(
        monkeypatch, make_config(minimal_config_dict), conn, fix=True
    )

    text = "\n".join(messages)
    assert "held by live process 4242" in text
    assert "left untouched" in text
    assert exit_code == 0
    assert not [cmd for cmd in conn.calls if cmd.startswith("rm ")]


def test_stale_lock_reported_and_removed(minimal_config_dict, monkeypatch):
    conn = clean_conn(lock="stale:999999")
    messages, exit_code = run_doctor(
        monkeypatch, make_config(minimal_config_dict), conn
    )
    assert exit_code == 1
    assert "stale holder file" in "\n".join(messages)

    conn = clean_conn(lock="stale:999999")
    messages, exit_code = run_doctor(
        monkeypatch, make_config(minimal_config_dict), conn, fix=True
    )
    assert exit_code == 0
    assert f"rm -f {APP_DIR}/store/lock.holder" in conn.calls


# ============================================================================
# Drifted units
# ============================================================================


def test_drifted_units_reinstalled(minimal_config_dict, monkeypatch):
    conn = clean_conn(
        installed={
            UNIT_NAME: "[Service]\nType=simple\nExecStart=/bin/false\n",
            "testapp-stray.service": "[Service]\nType=oneshot\n",
        }
    )
    messages, exit_code = run_doctor(
        monkeypatch, make_config(minimal_config_dict), conn, fix=True
    )

    assert exit_code == 0
    script = "\n".join(conn.calls)
    assert "base64 -d" in script and "/etc/systemd/system/" in script
    assert "sudo systemctl daemon-reload" in script
    # the drifted unit was rewritten, the stray one removed
    assert "testapp-stray.service" not in conn.installed
    assert "ExecStart=/bin/true" in conn.installed[UNIT_NAME]


# ============================================================================
# Permissions
# ============================================================================


def test_permissions_reported_and_fixed(minimal_config_dict, monkeypatch):
    modes = {
        APP_DIR: (0o0755, "deploy", APP),
        f"{APP_DIR}/current/.env": (0o0644, "deploy", APP),
        f"/run/{APP}": (0o0700, APP, APP),
    }
    conn = clean_conn(modes=modes)
    messages, exit_code = run_doctor(
        monkeypatch, make_config(minimal_config_dict), conn
    )
    text = "\n".join(messages)
    assert exit_code == 1
    assert "mode 0755" in text and "missing setgid bit" in text
    assert "mode 0644" in text
    assert "mode 0700; group cannot traverse/read it" in text
    assert mutating(conn.calls) == []

    conn = clean_conn(modes=modes)
    run_doctor(monkeypatch, make_config(minimal_config_dict), conn, fix=True)
    assert f"sudo chmod g+s {APP_DIR}" in conn.calls
    assert f"sudo chmod 0640 {APP_DIR}/current/.env" in conn.calls
    assert f"sudo chmod g+rx /run/{APP}" in conn.calls


# ============================================================================
# Caddy
# ============================================================================


def test_caddy_not_installed_is_a_clear_skip(minimal_config_dict, monkeypatch):
    conn = clean_conn(caddy=False)
    messages, exit_code = run_doctor(
        monkeypatch, make_config(minimal_config_dict), conn
    )
    assert exit_code == 0
    assert "caddy is not installed; config check skipped" in "\n".join(messages)
    assert not [cmd for cmd in conn.calls if "caddy validate" in cmd]


def test_caddy_validate_failure_reported(minimal_config_dict, monkeypatch):
    conn = clean_conn(validate_ok=False)
    messages, exit_code = run_doctor(
        monkeypatch, make_config(minimal_config_dict), conn
    )
    assert exit_code == 1
    assert "caddy validate failed" in "\n".join(messages)


# ============================================================================
# uv capability (relocatable venv)
# ============================================================================


def test_uv_relocatable_capability_probed_and_reported(
    minimal_config_dict, monkeypatch
):
    """One read-only probe of `uv venv --help`; a capable uv is OK."""
    conn = clean_conn()
    messages, exit_code = run_doctor(
        monkeypatch, make_config(minimal_config_dict), conn
    )

    assert exit_code == 0
    assert "uv venv supports --relocatable" in "\n".join(messages)
    help_calls = [cmd for cmd in conn.calls if cmd.startswith("uv venv --help")]
    assert help_calls == ["uv venv --help 2>&1"]


def test_uv_not_required_for_binary_install(minimal_config_dict, monkeypatch):
    """Binary mode never runs uv: no probe and no failure on an old uv."""
    config = make_config(minimal_config_dict)
    config.installation_mode = InstallationMode.BINARY
    conn = clean_conn(uv_relocatable=False)

    messages, exit_code = run_doctor(monkeypatch, config, conn)

    assert exit_code == 0
    assert "not used in binary mode" in "\n".join(messages)
    assert not [cmd for cmd in conn.calls if cmd.startswith("uv ")]


def test_old_uv_is_actionable_failure_and_never_repaired(
    minimal_config_dict, monkeypatch
):
    """An old uv (no --relocatable) fails the check with `fujin server
    upgrade` guidance; --fix must not try to upgrade anything."""
    conn = clean_conn(uv_relocatable=False)
    messages, exit_code = run_doctor(
        monkeypatch, make_config(minimal_config_dict), conn, fix=True
    )

    assert exit_code == 1
    text = "\n".join(messages)
    assert "--relocatable" in text
    assert "fujin server upgrade" in text
    assert mutating(conn.calls) == []


# ============================================================================
# Audit
# ============================================================================


def test_audit_digest_mismatch_reported(minimal_config_dict, monkeypatch):
    conn = clean_conn(
        audit=[
            {
                "timestamp": "2026-01-01T00:00:00+00:00",
                "operation": "deploy",
                "digest": DIGEST_ORPHAN,
            }
        ]
    )
    messages, exit_code = run_doctor(
        monkeypatch, make_config(minimal_config_dict), conn
    )
    assert exit_code == 1
    assert DIGEST_ORPHAN[:12] in "\n".join(messages)


def test_audit_legacy_records_without_digest_are_a_note(
    minimal_config_dict, monkeypatch
):
    conn = clean_conn(
        audit=[{"timestamp": "2026-01-01T00:00:00+00:00", "operation": "deploy"}]
    )
    messages, exit_code = run_doctor(
        monkeypatch, make_config(minimal_config_dict), conn
    )

    assert exit_code == 0
    assert "carry no release digest" in "\n".join(messages)
