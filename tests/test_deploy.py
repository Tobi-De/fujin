"""Tests for the release-tree deploy command (wave 3b, slices #76-#79).

No-op short-circuit, --force bypass, diff-based restart-set wiring,
replica convergence, state/GC updates, hook ordering and auto-rollback
semantics - all against a fake connection, no live SSH.

Tests pinning the retired zipapp/installer bundle flow were deleted with
that flow.
"""

from __future__ import annotations

import json
import sys
from unittest.mock import MagicMock

import cappa
import pytest

import fujin.commands.deploy as deploy_mod
import fujin.build as build_mod
import fujin.connection
import fujin.serverops
import fujin.transfer
from fujin.build import BuildResult
from fujin.commands.deploy import Deploy
from fujin.config import HealthCheckConfig
from fujin.errors import ConnectionError, DeploymentError, ImproperlyConfiguredError
from fujin.exit_codes import (
    EXIT_DEPLOY_FAILED,
    EXIT_DEPLOY_ROLLED_BACK,
    EXIT_HOST_UNREACHABLE,
    EXIT_ROLLBACK_FAILED,
    EXIT_USER_ABORTED,
)
import fujin.redaction
from fujin.manifest import InputFile, build_manifest, manifest_to_dict
from fujin.store import State

# ===========================================================================
# Helpers
# ===========================================================================

WEB_UNIT = "[Service]\nType=simple\nExecStart=/bin/true\n"

# Tree paths use deployed unit identities (app-prefixed); `worker` is
# intentionally a 2-replica template in every test config.
STAGING_FILES = {
    "systemd/testapp-web.service": WEB_UNIT,
    "systemd/testapp-worker@.service": WEB_UNIT,
    "assets/logo.png": b"png",
    "bin/testapp-1.0.0-py3-none-any.whl": b"wheel",
}
WORKER_REPLICAS = {"worker": 2}


def build_result_for(tmp_path, files=STAGING_FILES, dist_sha="dist-000"):
    staging = tmp_path / "staging"
    for path, content in files.items():
        full = staging / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(content if isinstance(content, bytes) else content.encode())
    return staging, BuildResult(
        staging_dir=staging,
        bundle_key="bundle-key",
        files={
            p: InputFile(content=c if isinstance(c, bytes) else c.encode())
            for p, c in files.items()
        },
        dist_path=staging / "bin/testapp-1.0.0-py3-none-any.whl",
        dist_sha256=dist_sha,
    )


def manifest_for(version="0.9.0", dist="dist-000", with_env=False, files=None):
    specs = {
        p: InputFile(content=c if isinstance(c, bytes) else c.encode())
        for p, c in (files if files is not None else STAGING_FILES).items()
    }
    if with_env:
        specs[".env"] = InputFile(content=b"")
    return build_manifest(
        app_name="testapp",
        version=version,
        config_fingerprint="fp0",
        dist_sha256=dist,
        files=specs,
    )


def manifest_json(version="0.9.0", dist="dist-000", with_env=False, files=None):
    return json.dumps(
        manifest_to_dict(
            manifest_for(version=version, dist=dist, with_env=with_env, files=files),
            include_created_at=False,
        )
    )


class FakeConn:
    """SSH connection fake: `responses` maps cmd-substring -> (out, ok)."""

    def __init__(self, responses=None, events=None):
        self.calls: list[tuple[str, dict]] = []
        self.responses = responses or []
        self.events = events
        self.uploads: list[tuple[str, str]] = []

    def run(self, cmd, **kw):
        self.calls.append((cmd, kw))
        if self.events is not None:
            self.events.append(("conn", cmd))
        for marker, (out, ok) in self.responses:
            if marker in cmd:
                return out, ok
        # Existence checks are path-aware: a test -d/-f probe hits iff some
        # registered marker equals the probed path or lives inside it (a
        # blind ok=True would spuriously trigger the release-reuse path).
        if cmd.startswith("test -d ") or cmd.startswith("test -f "):
            target = cmd.split(" ", 2)[2].split()[0].strip("'\"")
            return "", bool(
                any(
                    m == target or m.startswith(target + "/") for m, _ in self.responses
                )
            )
        return "", True

    def put(self, local, remote, verify=False):
        self.uploads.append((local, remote))
        if self.events is not None:
            self.events.append(("put", local, remote))

    def append_response(self, marker, out, ok=True):
        self.responses.append((marker, (out, ok)))

    def get(self, remote, local, verify=False):
        """Fake SCP download: answers from the same marker registry."""
        self.calls.append((remote, {"get": True}))
        for marker, (out, ok) in self.responses:
            if marker in remote:
                if not ok:
                    raise FileNotFoundError(remote)
                with open(local, "w") as fh:
                    fh.write(out)
                return
        raise FileNotFoundError(remote)


def invoke(cmd) -> int:
    """Run a deploy; return its exit code (0 when it completed normally)."""
    try:
        cmd()
        return 0
    except SystemExit as exc:
        code = exc.code
        if code not in (0, None):
            import traceback

            traceback.print_exception(exc, file=sys.stderr)
        return 0 if code is None else code
    except Exception:
        import traceback

        traceback.print_exc()
        raise


def setup_deploy(
    monkeypatch,
    minimal_config,
    tmp_path,
    *,
    files=STAGING_FILES,
    state=None,
    prev_manifest=None,
    verify_ok=True,
    replicas=None,
    hooks=None,
    is_failed="active\nactive\n",
):
    """Mock the whole deploy flow. Returns ``(cmd, spies)``."""
    if state is None:
        state = State()
    staging, result = build_result_for(tmp_path, files)
    monkeypatch.setattr(deploy_mod.build, "run_build", lambda *a, **k: result)
    monkeypatch.setattr(deploy_mod, "get_git_short_hash", lambda: None)

    events: list = []
    conn = FakeConn(
        responses=[
            ("list-unit-files", ("", True)),
            ("is-failed", (is_failed + "active\nactive\nactive\n", True)),
        ],
        events=events,
    )
    conn_ctx = MagicMock()
    conn_ctx.return_value.__enter__.return_value = conn
    conn_ctx.return_value.__exit__.return_value = None
    monkeypatch.setattr(fujin.connection, "connection", conn_ctx)

    monkeypatch.setattr(fujin.serverops, "ensure_layout", MagicMock())
    monkeypatch.setattr(fujin.transfer, "sweep_stale_staging", lambda *a, **k: [])
    monkeypatch.setattr(fujin.serverops, "acquire_lock", lambda *a, **k: "flock:1")
    released = []
    monkeypatch.setattr(
        fujin.serverops, "release_lock", lambda _c, _a, t: released.append(t)
    )
    monkeypatch.setattr(fujin.serverops, "read_state", lambda _c, _a: state)
    monkeypatch.setattr(
        fujin.serverops, "read_current_manifest", lambda _c, _a: prev_manifest
    )
    monkeypatch.setattr(fujin.serverops, "install_code_into_staging", MagicMock())
    link_unchanged = MagicMock(return_value=(0, []))
    monkeypatch.setattr(fujin.serverops, "link_unchanged", link_unchanged)
    monkeypatch.setattr(fujin.serverops, "hash_server_built", lambda *a, **k: {})
    install_units = MagicMock()
    monkeypatch.setattr(fujin.serverops, "install_units", install_units)
    monkeypatch.setattr(fujin.serverops, "set_envfile_mode", MagicMock())
    write_states = []
    monkeypatch.setattr(
        fujin.serverops, "write_state", lambda _c, _a, s: write_states.append(s)
    )
    stop_and_disable = MagicMock(return_value=True)
    monkeypatch.setattr(fujin.serverops, "stop_and_disable", stop_and_disable)
    enable_and_start = MagicMock(return_value=True)
    monkeypatch.setattr(fujin.serverops, "enable_and_start", enable_and_start)
    monkeypatch.setattr(
        fujin.serverops, "install_caddyfile", MagicMock(return_value=True)
    )

    monkeypatch.setattr(fujin.transfer, "rsync_to_staging", MagicMock(return_value=[]))
    verify_payload = dict(
        ok=verify_ok,
        missing=[] if verify_ok else ["assets/logo.png"],
        mismatch={},
        link_mismatch={},
        mode_mismatch={},
    )
    verify = MagicMock(return_value=fujin.transfer.VerifyResult(**verify_payload))
    monkeypatch.setattr(fujin.transfer, "verify_staging", verify)

    commit = MagicMock(side_effect=lambda *a, **k: events.append(("commit", (a, k))))
    monkeypatch.setattr(fujin.transfer, "commit_release", commit)
    swap = MagicMock(side_effect=lambda *a, **k: events.append(("swap", (a, k))))
    monkeypatch.setattr(fujin.transfer, "swap_current", swap)
    cleanup = MagicMock()
    monkeypatch.setattr(fujin.transfer, "cleanup_staging", cleanup)

    log_calls = []
    monkeypatch.setattr(
        deploy_mod, "log_operation", lambda *a, **k: log_calls.append((a, k))
    )

    if replicas is not None:
        minimal_config.replicas = replicas
    else:
        minimal_config.replicas = dict(WORKER_REPLICAS)
    if hooks is not None:
        minimal_config.hooks_config = hooks

    cmd = Deploy(no_input=True)
    cmd.config = minimal_config
    import sys as _sys

    class _Printer:
        def success(self, m):
            print("OK:", m, file=_sys.stderr)

        def error(self, m):
            print("ERR:", m, file=_sys.stderr)

        def warning(self, m):
            print("WARN:", m, file=_sys.stderr)

        def info(self, m):
            print("INFO:", m, file=_sys.stderr)

        def output(self, m):
            print("OUT:", m, file=_sys.stderr)

        def critical(self, m):
            print("CRIT:", m, file=_sys.stderr)

        def dim(self, text):
            return text

        def link(self, url, text=None):
            return text or url

    cmd.output = _Printer()
    for _n in ("ensure_layout",):
        pass

    spies = {
        "conn": conn,
        "conn_ctx": conn_ctx,
        "events": events,
        "staging_dir": staging,
        "rsync": fujin.transfer.rsync_to_staging,
        "commit": commit,
        "swap": swap,
        "verify": verify,
        "install_units": install_units,
        "write_states": write_states,
        "log_calls": log_calls,
        "enable_and_start": enable_and_start,
        "stop_and_disable": stop_and_disable,
        "cleanup_staging": cleanup,
        "link_unchanged": link_unchanged,
        "released": released,
    }
    return cmd, spies


def systemctl_restart_calls(conn):
    return [
        c
        for c, _ in conn.calls
        if "systemctl" in c
        and "restart" in c
        and "is-failed" not in c
        and "reset-failed" not in c
    ]


# ===========================================================================
# No-op deploy (#78)
# ===========================================================================


def test_noop_short_circuit_skips_transfer_install_restart(
    monkeypatch, minimal_config, tmp_path
):
    monkeypatch.setattr(deploy_mod.m, "release_digest", lambda *_a, **_k: "digest-x")
    state = State(current="prev", client_digest="digest-x")
    cmd, spies = setup_deploy(monkeypatch, minimal_config, tmp_path, state=state)
    spies["conn"].append_response(
        "list-unit-files", "testapp-worker@1.service template"
    )
    cmd()

    spies["rsync"].assert_not_called()
    spies["commit"].assert_not_called()
    spies["swap"].assert_not_called()
    spies["install_units"].assert_not_called()
    assert spies["write_states"] == []
    operations = [k.get("operation") or a[2] for a, k in spies["log_calls"]]
    assert "noop" in operations
    assert spies["released"] == ["flock:1"]


def test_noop_still_runs_replica_convergence(monkeypatch, minimal_config, tmp_path):
    monkeypatch.setattr(deploy_mod.m, "release_digest", lambda *_a, **_k: "digest-x")
    state = State(current="prev", client_digest="digest-x")
    cmd, spies = setup_deploy(monkeypatch, minimal_config, tmp_path, state=state)
    spies["conn"].responses = [
        ("list-unit-files", ("testapp-worker@1.service enabled\n", True)),
        ("list-units", ("testapp-worker@1.service loaded active running\n", True)),
    ]
    cmd()

    started = spies["enable_and_start"].call_args.args[1]
    assert started == ["testapp-worker@2.service"]
    # the already-present instance is not touched, nothing to stop
    stopped = [str(c) for c in spies["stop_and_disable"].call_args_list]
    assert stopped == []
    # bare templates are never enabled directly
    assert not any('@.service",' in u for u in started)


def test_force_bypasses_noop(monkeypatch, minimal_config, tmp_path):
    monkeypatch.setattr(deploy_mod.m, "release_digest", lambda *_a, **_k: "digest-x")
    state = State(current="prev", client_digest="digest-x")
    cmd, spies = setup_deploy(monkeypatch, minimal_config, tmp_path, state=state)
    cmd.force = True
    invoke(cmd)

    spies["rsync"].assert_called()
    spies["commit"].assert_called()
    spies["swap"].assert_called()


def test_convergence_scale_down_stops_highest_first(
    monkeypatch, minimal_config, tmp_path
):
    monkeypatch.setattr(deploy_mod.m, "release_digest", lambda *_a, **_k: "digest-x")
    state = State(current="prev", client_digest="digest-x")
    cmd, spies = setup_deploy(
        monkeypatch, minimal_config, tmp_path, state=state, replicas={"worker": 3}
    )
    spies["conn"].responses = [
        (
            "list-unit-files",
            (
                "\n".join(f"testapp-worker@{i}.service enabled" for i in range(1, 6)),
                True,
            ),
        ),
        (
            "list-units",
            (
                "\n".join(
                    f"testapp-worker@{i}.service loaded active running"
                    for i in range(1, 6)
                ),
                True,
            ),
        ),
    ]
    cmd()

    stopped = spies["stop_and_disable"].call_args.args[1]
    assert stopped == ["testapp-worker@5.service", "testapp-worker@4.service"]
    # nothing is missing/stopped-but-enabled: nothing to start
    spies["enable_and_start"].assert_not_called()


def test_noop_convergence_restarts_stopped_but_enabled_instance(
    monkeypatch, minimal_config, tmp_path
):
    monkeypatch.setattr(deploy_mod.m, "release_digest", lambda *_a, **_k: "digest-x")
    state = State(current="prev", client_digest="digest-x")
    cmd, spies = setup_deploy(
        monkeypatch, minimal_config, tmp_path, state=state, replicas={"worker": 2}
    )
    spies["conn"].responses = [
        (
            "list-unit-files",
            (
                "testapp-worker@1.service enabled\ntestapp-worker@2.service enabled\n",
                True,
            ),
        ),
        # worker@1 was stopped by hand and is therefore not active
        ("list-units", ("testapp-worker@2.service loaded active running\n", True)),
    ]
    cmd()

    started = spies["enable_and_start"].call_args.args[1]
    assert started == ["testapp-worker@1.service"]
    spies["stop_and_disable"].assert_not_called()


def test_convergence_scale_up_starts_only_missing_instances(
    monkeypatch, minimal_config, tmp_path
):
    monkeypatch.setattr(deploy_mod.m, "release_digest", lambda *_a, **_k: "digest-x")
    state = State(current="prev", client_digest="digest-x")
    cmd, spies = setup_deploy(
        monkeypatch, minimal_config, tmp_path, state=state, replicas={"worker": 3}
    )
    spies["conn"].responses = [
        (
            "list-unit-files",
            ("testapp-worker@1.service enabled\n", True),
        ),
        ("list-units", ("testapp-worker@1.service loaded active running\n", True)),
    ]
    cmd()

    started = spies["enable_and_start"].call_args.args[1]
    assert started == ["testapp-worker@2.service", "testapp-worker@3.service"]
    spies["stop_and_disable"].assert_not_called()


def test_convergence_stops_leftovers_when_template_removed(
    monkeypatch, minimal_config, tmp_path
):
    monkeypatch.setattr(deploy_mod.m, "release_digest", lambda *_a, **_k: "digest-x")
    state = State(current="prev", client_digest="digest-x")
    cmd, spies = setup_deploy(
        monkeypatch,
        minimal_config,
        tmp_path,
        state=state,
        files={"systemd/testapp-web.service": WEB_UNIT},
    )
    spies["conn"].responses = [
        ("list-unit-files", ("testapp-worker@1.service enabled\n", True)),
        ("list-units", ("testapp-worker@2.service loaded active running\n", True)),
    ]
    cmd()

    stopped = spies["stop_and_disable"].call_args.args[1]
    assert stopped == ["testapp-worker@2.service", "testapp-worker@1.service"]
    spies["enable_and_start"].assert_not_called()


def test_convergence_starts_running_but_disabled_instance(
    monkeypatch, minimal_config, tmp_path
):
    monkeypatch.setattr(deploy_mod.m, "release_digest", lambda *_a, **_k: "digest-x")
    state = State(current="prev", client_digest="digest-x")
    cmd, spies = setup_deploy(
        monkeypatch, minimal_config, tmp_path, state=state, replicas={"worker": 2}
    )
    spies["conn"].responses = [
        # worker@2 runs but is not enabled -> needs `enable --now`
        ("list-unit-files", ("testapp-worker@1.service enabled\n", True)),
        (
            "list-units",
            (
                "testapp-worker@1.service loaded active running\n"
                "testapp-worker@2.service loaded active running\n",
                True,
            ),
        ),
    ]
    cmd()

    started = spies["enable_and_start"].call_args.args[1]
    assert started == ["testapp-worker@2.service"]
    spies["stop_and_disable"].assert_not_called()


def test_convergence_stops_non_numeric_instance_without_crashing(
    monkeypatch, minimal_config, tmp_path
):
    monkeypatch.setattr(deploy_mod.m, "release_digest", lambda *_a, **_k: "digest-x")
    state = State(current="prev", client_digest="digest-x")
    cmd, spies = setup_deploy(
        monkeypatch, minimal_config, tmp_path, state=state, replicas={"worker": 1}
    )
    spies["conn"].responses = [
        (
            "list-unit-files",
            (
                "testapp-worker@1.service enabled\n"
                "testapp-worker@canary.service enabled\n",
                True,
            ),
        ),
        (
            "list-units",
            (
                "testapp-worker@1.service loaded active running\n"
                "testapp-worker@canary.service loaded active running\n",
                True,
            ),
        ),
    ]
    cmd()

    stopped = spies["stop_and_disable"].call_args.args[1]
    assert stopped == ["testapp-worker@canary.service"]
    spies["enable_and_start"].assert_not_called()


# ===========================================================================
# Diff-based restarts (#79)
# ===========================================================================


def test_env_change_restarts_env_fed_units(monkeypatch, minimal_config, tmp_path):
    # The previous release has no .env entry; this deploy adds one -> env-fed
    # service units restart.
    cmd, spies = setup_deploy(
        monkeypatch,
        minimal_config,
        tmp_path,
        state=State(current="prev", client_digest="old"),
        prev_manifest=manifest_for(with_env=False),
    )
    invoke(cmd)

    restarts = systemctl_restart_calls(spies["conn"])
    assert restarts, "env-fed units must be restarted when the env changes"
    assert all("reload-or-restart" in c for c in restarts)
    joined = "\n".join(restarts)
    assert "testapp-web.service" in joined
    assert "testapp-worker@*.service" in joined


def test_asset_only_change_restarts_nothing(monkeypatch, minimal_config, tmp_path):
    # prev has one asset the new tree drops: only asset entries changed.
    # Every release ships a .env, so the previous manifest must carry the
    # one this deploy re-renders (same content -> no env change); otherwise
    # adding .env would (correctly) restart the env-fed units.
    prev_files = dict(STAGING_FILES)
    prev_files["assets/other.png"] = b"other"
    prev = manifest_for(with_env=True, files=prev_files)
    cmd, spies = setup_deploy(
        monkeypatch,
        minimal_config,
        tmp_path,
        state=State(current="prev", client_digest="old"),
        prev_manifest=prev,
    )
    invoke(cmd)
    assert systemctl_restart_calls(spies["conn"]) == []


def test_unit_change_restarts_only_that_unit(monkeypatch, minimal_config, tmp_path):
    prev_files = dict(STAGING_FILES)
    prev_files["systemd/testapp-worker@.service"] = (
        "[Service]\nType=simple\nExecStart=/old\n"
    )
    # .env is shipped by every release and unchanged here; with_env=True
    # keeps the diff to the worker unit alone.
    prev = manifest_for(with_env=True, files=prev_files)
    cmd, spies = setup_deploy(
        monkeypatch,
        minimal_config,
        tmp_path,
        state=State(current="prev", client_digest="old"),
        prev_manifest=prev,
    )
    invoke(cmd)

    restarts = systemctl_restart_calls(spies["conn"])
    assert restarts
    joined = "\n".join(restarts)
    assert "testapp-worker@*.service" in joined
    assert "testapp-web.service" not in joined
    assert all("reload-or-restart" in c for c in restarts)


def test_code_change_restarts_all_app_units(monkeypatch, minimal_config, tmp_path):
    # same tree, different dist hash -> code changed via meta
    prev = manifest_for(with_env=False, dist="older-dist")
    cmd, spies = setup_deploy(
        monkeypatch,
        minimal_config,
        tmp_path,
        state=State(current="prev", client_digest="old"),
        prev_manifest=prev,
        files=STAGING_FILES,
    )
    invoke(cmd)

    restarts = systemctl_restart_calls(spies["conn"])
    joined = "\n".join(restarts)
    assert "testapp-web.service" in joined
    assert "testapp-worker@*.service" in joined


def test_full_restart_restarts_everything(monkeypatch, minimal_config, tmp_path):
    cmd, spies = setup_deploy(
        monkeypatch,
        minimal_config,
        tmp_path,
        state=State(current="prev", client_digest="old"),
        prev_manifest=manifest_for(with_env=True, files=STAGING_FILES),
    )
    cmd.full_restart = True
    invoke(cmd)

    restarts = systemctl_restart_calls(spies["conn"])
    joined = "\n".join(restarts)
    assert all("systemctl restart" in c for c in restarts)
    assert "testapp-web.service" in joined
    assert "testapp-worker@*.service" in joined


# ===========================================================================
# State / GC / audit
# ===========================================================================


def test_deploy_records_state_and_gcs_old_releases(
    monkeypatch, minimal_config, tmp_path
):
    state = State(current="old-a", previous=[], deploys=[])
    minimal_config.versions_to_keep = 2
    cmd, spies = setup_deploy(
        monkeypatch,
        minimal_config,
        tmp_path,
        state=state,
        prev_manifest=manifest_for(),
    )
    spies["conn"].append_response("ls -1t releases/", "old-b\nold-a\n")
    invoke(cmd)

    written = spies["write_states"][0]
    assert written.previous == ["old-a"]
    assert written.deploys[-1].result == "ok"

    rms = [c for c, _ in spies["conn"].calls if "rm -rf" in c and "releases/" in c]
    assert any("old-b" in c for c in rms), rms
    assert not any("old-a" in c for c in rms), rms
    operations = [k.get("operation") or a[2] for a, k in spies["log_calls"]]
    assert "deploy" in operations
    assert spies["released"] == ["flock:1"]


# ===========================================================================
# Hooks
# ===========================================================================


def test_hook_ordering_and_post_install_target(monkeypatch, minimal_config, tmp_path):
    from fujin.config import HooksConfig

    hooks = HooksConfig(
        pre_install=["echo PRE"],
        post_install=["$FUJIN_APPDIR/migrate"],
        post_start=["echo POST"],
    )
    cmd, spies = setup_deploy(
        monkeypatch,
        minimal_config,
        tmp_path,
        state=State(current="prev", client_digest="old"),
        prev_manifest=manifest_for(),
        hooks=hooks,
    )
    invoke(cmd)

    markers = []
    for entry in spies["events"]:
        if entry[0] == "commit":
            markers.append("commit")
        elif entry[0] == "swap":
            markers.append("swap")
        elif entry[0] == "conn":
            c = entry[1]
            if "echo PRE" in c:
                markers.append("pre")
            elif "migrate" in c:
                markers.append("post")
            elif "echo POST" in c:
                markers.append("post_start")
    assert markers == ["pre", "commit", "post", "swap", "post_start"]

    post_cmds = [c for c, _ in spies["conn"].calls if "migrate" in c]
    assert post_cmds, "post_install hook must run"


def test_hook_commands_interpolate_app_dir_to_new_release(
    monkeypatch, minimal_config, tmp_path
):
    from fujin.config import HooksConfig

    hooks = HooksConfig(post_install=["{app_dir}/bin/migrate"])
    cmd, spies = setup_deploy(
        monkeypatch,
        minimal_config,
        tmp_path,
        state=State(current="prev", client_digest="old"),
        prev_manifest=manifest_for(),
        hooks=hooks,
    )
    invoke(cmd)

    post_cmds = [c for c, _ in spies["conn"].calls if "migrate" in c]
    assert post_cmds
    assert any(
        f"{minimal_config.app_dir}/releases/" in c and "/bin/migrate" in c
        for c in post_cmds
    )
    assert any("/releases/" in c and ".env" in c for c in post_cmds)


# ===========================================================================
# Start failure / auto rollback (exit-code taxonomy: 3/4/6)
# ===========================================================================


def _setup_failing_deploy(
    monkeypatch, minimal_config, tmp_path, *, no_rollback, prev_digest="prev"
):
    state = State(current=prev_digest, previous=[])
    cmd, spies = setup_deploy(
        monkeypatch,
        minimal_config,
        tmp_path,
        state=state,
        prev_manifest=manifest_for(),
        is_failed="failed\nfailed\n",
    )
    spies["conn"].append_response(
        f"/opt/fujin/testapp/releases/{prev_digest}/manifest.json",
        manifest_json(version="0.9.0"),
    )
    cmd.no_rollback = no_rollback
    return cmd, spies


def test_service_start_failure_auto_rolls_back_and_exits_3(
    monkeypatch, minimal_config, tmp_path
):
    cmd, spies = _setup_failing_deploy(
        monkeypatch, minimal_config, tmp_path, no_rollback=False
    )
    assert invoke(cmd) == EXIT_DEPLOY_ROLLED_BACK

    swap_digests = [args[0][2] for args in spies["swap"].call_args_list]
    assert "prev" in swap_digests
    written = spies["write_states"][0]
    assert written.current == "prev"
    assert written.deploys[-1].result == "rollback"


def test_restart_command_failure_rolls_back(monkeypatch, minimal_config, tmp_path):
    """#76 step table: a failing `systemctl restart` (not just failed units)
    must trigger the same interactive auto-rollback after the swap."""
    state = State(current="prev", previous=[])
    cmd, spies = setup_deploy(
        monkeypatch,
        minimal_config,
        tmp_path,
        state=state,
        prev_manifest=manifest_for(dist="dist-old"),
        is_failed="active\nactive\n",
    )
    spies["conn"].append_response(
        "/opt/fujin/testapp/releases/prev/manifest.json",
        manifest_json(version="0.9.0"),
    )
    spies["conn"].append_response("systemctl reload-or-restart", "", False)
    assert invoke(cmd) == EXIT_DEPLOY_ROLLED_BACK

    swap_digests = [args[0][2] for args in spies["swap"].call_args_list]
    assert "prev" in swap_digests
    written = spies["write_states"][0]
    assert written.current == "prev"
    assert written.deploys[-1].result == "rollback"


def test_no_rollback_exits_4_without_swapping(monkeypatch, minimal_config, tmp_path):
    cmd, spies = _setup_failing_deploy(
        monkeypatch, minimal_config, tmp_path, no_rollback=True
    )
    assert invoke(cmd) == EXIT_DEPLOY_FAILED
    # The new release was activated (swapped in); --no-rollback must NOT
    # swap back to the previous digest.
    rollback_swaps = [
        a for args, _k in spies["swap"].call_args_list for a in args if a == "prev"
    ]
    assert rollback_swaps == []


def test_service_failure_without_previous_release_exits_4(
    monkeypatch, minimal_config, tmp_path
):
    cmd, _spies = setup_deploy(
        monkeypatch,
        minimal_config,
        tmp_path,
        state=State(),
        prev_manifest=None,
        is_failed="failed\nfailed\n",
    )
    cmd.no_rollback = False
    assert invoke(cmd) == EXIT_DEPLOY_FAILED


def test_service_failure_declined_rollback_exits_1(
    monkeypatch, minimal_config, tmp_path
):
    cmd, spies = _setup_failing_deploy(
        monkeypatch, minimal_config, tmp_path, no_rollback=False
    )
    cmd.no_input = False
    monkeypatch.setattr(deploy_mod.Confirm, "ask", lambda *a, **k: False)

    assert invoke(cmd) == EXIT_USER_ABORTED
    rollback_swaps = [
        a for args, _k in spies["swap"].call_args_list for a in args if a == "prev"
    ]
    assert rollback_swaps == []


def test_auto_rollback_step_failure_exits_6(monkeypatch, minimal_config, tmp_path):
    """A rollback whose previous release cannot be read is a failed rollback
    (6), not a plain deploy failure - a second rollback is never attempted."""
    cmd, spies = setup_deploy(
        monkeypatch,
        minimal_config,
        tmp_path,
        state=State(current="prev", previous=[]),
        prev_manifest=manifest_for(),
        is_failed="failed\nfailed\n",
    )
    cmd.no_rollback = False
    # No response for the previous release's manifest: the rollback step
    # fails after the failure was surfaced.
    assert invoke(cmd) == EXIT_ROLLBACK_FAILED
    written = spies["write_states"]
    assert written == []  # rollback never recorded success


def test_failed_post_install_hook_exits_4_before_activation(
    monkeypatch, minimal_config, tmp_path
):
    """A failing post_install hook aborts with 4 while the old release is
    still live: nothing was activated, so there is nothing to roll back."""
    from fujin.config import HooksConfig

    hooks = HooksConfig(post_install=["exit 1"])
    cmd, spies = setup_deploy(
        monkeypatch,
        minimal_config,
        tmp_path,
        state=State(current="prev", client_digest="old"),
        prev_manifest=manifest_for(),
        hooks=hooks,
    )
    spies["conn"].append_response("exit 1", "", False)

    assert invoke(cmd) == EXIT_DEPLOY_FAILED
    spies["swap"].assert_not_called()
    assert spies["cleanup_staging"].called


def test_unreachable_host_during_transfer_exits_5(
    monkeypatch, minimal_config, tmp_path
):
    """The rsync host classification (5) survives the deploy error wrapper."""
    from fujin.transfer import HostUnreachableError

    cmd, spies = setup_deploy(
        monkeypatch,
        minimal_config,
        tmp_path,
        state=State(current="prev", client_digest="old"),
        prev_manifest=manifest_for(),
    )
    spies["rsync"].side_effect = HostUnreachableError("ssh: connect: refused")

    assert invoke(cmd) == EXIT_HOST_UNREACHABLE
    spies["swap"].assert_not_called()
    assert spies["cleanup_staging"].called


def test_mid_deploy_transport_loss_exits_5(monkeypatch, minimal_config, tmp_path):
    """A ConnectionError raised while executing a remote step propagates as
    5 through the deploy handlers, even when staging cleanup also fails."""
    from fujin.config import HooksConfig
    from fujin.errors import ConnectionError

    hooks = HooksConfig(pre_install=["TRANSPORT_LOSS"])
    cmd, spies = setup_deploy(
        monkeypatch,
        minimal_config,
        tmp_path,
        state=State(current="prev", client_digest="old"),
        prev_manifest=manifest_for(),
        hooks=hooks,
    )
    real_run = spies["conn"].run

    def flaky_run(command, **kwargs):
        if "TRANSPORT_LOSS" in command:
            raise ConnectionError("SSH transport failed mid-command")
        return real_run(command, **kwargs)

    spies["conn"].run = flaky_run
    # Cleanup itself fails: that must not change the propagated code.
    spies["cleanup_staging"].side_effect = RuntimeError("cleanup exploded")

    assert invoke(cmd) == EXIT_HOST_UNREACHABLE
    spies["cleanup_staging"].assert_called()
    spies["swap"].assert_not_called()


def test_cleanup_failure_does_not_mask_deploy_failure(
    monkeypatch, minimal_config, tmp_path
):
    """A cleanup failure must not replace the original taxonomy code (4).

    Cleanup runs through ``conn.run``, so on a dead transport it raises
    ``ConnectionError`` (a ``cappa.Exit``/``SystemExit``, not ``Exception``):
    that must be swallowed, not override the deploy failure."""
    from fujin.config import HooksConfig
    from fujin.errors import ConnectionError

    hooks = HooksConfig(post_install=["exit 1"])
    cmd, spies = setup_deploy(
        monkeypatch,
        minimal_config,
        tmp_path,
        state=State(current="prev", client_digest="old"),
        prev_manifest=manifest_for(),
        hooks=hooks,
    )
    spies["conn"].append_response("exit 1", "", False)
    spies["cleanup_staging"].side_effect = ConnectionError(
        "SSH transport failed while cleaning up"
    )

    assert invoke(cmd) == EXIT_DEPLOY_FAILED
    spies["cleanup_staging"].assert_called()


def test_failed_post_start_hook_warns_and_deploy_succeeds(
    monkeypatch, minimal_config, tmp_path
):
    """post_start runs after activation: its failure is a warning, the
    deploy (and the recorded state) still succeeds."""
    from fujin.config import HooksConfig

    hooks = HooksConfig(post_start=["exit 1"])
    cmd, spies = setup_deploy(
        monkeypatch,
        minimal_config,
        tmp_path,
        state=State(current="prev", client_digest="old"),
        prev_manifest=manifest_for(),
        hooks=hooks,
    )
    spies["conn"].append_response("exit 1", "", False)
    warnings: list[str] = []
    cmd.output.warning = warnings.append

    assert invoke(cmd) == 0
    assert any("post-start" in w for w in warnings), warnings
    assert len(spies["write_states"]) == 1
    assert spies["write_states"][0].current


# ===========================================================================
# Regression: .env bytes / transfer excludes / rollback target
# ===========================================================================


def test_env_bytes_written_to_staging_before_transfer(
    monkeypatch, minimal_config, tmp_path
):
    """Blocker regression: the rendered .env must exist on disk inside the
    staging tree before the transfer runs, in both the rsync and SCP-fallback
    transfer paths - rsync mirrors the on-disk tree and the SCP fallback
    walks it, so a missing file broke the server-side chmod 0640 and verify."""
    minimal_config.hosts[0].env_content = "SECRET_TOKEN=1\n"
    cmd, spies = setup_deploy(
        monkeypatch, minimal_config, tmp_path, state=State(), prev_manifest=None
    )
    captured = {}

    def fake_rsync(*args, **kwargs):
        captured["env_at_transfer"] = (spies["staging_dir"] / ".env").read_bytes()
        return []

    spies["rsync"].side_effect = fake_rsync
    assert invoke(cmd) == 0

    # Config strips env_content before deployment.
    assert captured["env_at_transfer"] == b"SECRET_TOKEN=1"
    assert spies["rsync"].call_args.kwargs["exclude"] == {"built.json"}


def test_merged_manifest_uploaded_via_scp_not_command_string(
    monkeypatch, minimal_config, tmp_path
):
    """Blocker regression: the final (server-merged) manifest can be hundreds
    of KB of venv entries; sending it inside a shell command string hung the
    SSH channel forever. It must ride via conn.put instead."""
    cmd, spies = setup_deploy(
        monkeypatch, minimal_config, tmp_path, state=State(), prev_manifest=None
    )
    assert invoke(cmd) == 0
    manifest_uploads = [
        remote
        for (_local, remote) in spies["conn"].uploads
        if remote.endswith("manifest.json")
    ]
    # exactly one upload, into the remote staging dir, via put (never a shell command)
    assert len(manifest_uploads) == 1
    assert "/releases/.staging-" in manifest_uploads[0]
    assert not any(
        cmd.find("manifest.json") != -1 and "echo" in cmd
        for cmd, _kw in spies["conn"].calls
    )


def test_auto_rollback_targets_release_live_before_this_deploy(
    monkeypatch, minimal_config, tmp_path
):
    """Blocker regression: state.json is written only after a successful
    deploy, so on a failed deploy the previous live release is still
    ``state.current`` - even when state.previous points two deploys back."""
    conn_state = State(current="third", previous=["second"])
    cmd, spies = setup_deploy(
        monkeypatch,
        minimal_config,
        tmp_path,
        state=conn_state,
        prev_manifest=manifest_for(),
        is_failed="failed\nfailed\n",
    )
    spies["conn"].append_response(
        "/opt/fujin/testapp/releases/third/manifest.json",
        manifest_json(version="1.0.0"),
    )
    assert invoke(cmd) == EXIT_DEPLOY_ROLLED_BACK

    swap_digests = [args[0][2] for args in spies["swap"].call_args_list]
    assert "third" in swap_digests
    assert "second" not in swap_digests
    written = spies["write_states"][0]
    assert written.current == "third"


# ===========================================================================
# Secrets redaction + fail-closed invariant (#87)
# ===========================================================================

from pathlib import Path  # noqa: E402  (appended for #87 tests)

from fujin.config import SecretConfig  # noqa: E402  (appended for #87 tests)

SENTINEL = "sentinel-hunter-4242d0"


class _CapturingPrinter:
    """Record every message an output object asks to display."""

    def __init__(self):
        self.lines: list[str] = []

    def success(self, m):
        print("OK:", m, file=sys.stderr)
        self.lines.append(m)

    def error(self, m):
        print("ERR:", m, file=sys.stderr)
        self.lines.append(m)

    def warning(self, m):
        print("WARN:", m, file=sys.stderr)
        self.lines.append(m)

    def info(self, m):
        print("INFO:", m, file=sys.stderr)
        self.lines.append(m)

    def output(self, m):
        print("OUT:", m, file=sys.stderr)
        self.lines.append(m)

    def critical(self, m):
        print("CRIT:", m, file=sys.stderr)
        self.lines.append(m)

    def dim(self, text):
        return text

    def link(self, url, text=None):
        return text or url


def _secrets_setup(monkeypatch, minimal_config, tmp_path, **kwargs):
    """Deploy harness with a sentinel secret resolved by the (faked) adapter.

    Returns (cmd, spies, printer). The faked ``resolve_secrets`` is the only
    place the raw sentinel value exists; everything else must see the
    ``<$STRIPE_TOKEN>`` placeholder or silence.
    """
    monkeypatch.setattr(
        deploy_mod,
        "resolve_secrets",
        lambda env, sc: env.replace("$STRIPE_TOKEN", SENTINEL),
    )
    minimal_config.secret_config = SecretConfig(adapter="test")
    minimal_config.hosts[0].env_content = "STRIPE_TOKEN=$STRIPE_TOKEN\nDEBUG=1"
    cmd, spies = setup_deploy(monkeypatch, minimal_config, tmp_path, **kwargs)
    printer = _CapturingPrinter()
    cmd.output = printer
    return cmd, spies, printer


def _assert_no_sentinel(printer, capsys, expect_placeholder=False):
    """No captured output (ours or pytest's) may contain the sentinel."""
    displayed = "\n".join(printer.lines)
    out, err = capsys.readouterr()
    assert SENTINEL not in displayed
    assert SENTINEL not in out
    assert SENTINEL not in err
    if expect_placeholder:
        assert "<$STRIPE_TOKEN>" in displayed


def test_secrets_never_leak_on_successful_deploy(
    monkeypatch, minimal_config, tmp_path, capsys
):
    cmd, spies, printer = _secrets_setup(
        monkeypatch, minimal_config, tmp_path, state=State()
    )
    assert invoke(cmd) == 0
    _assert_no_sentinel(printer, capsys)


def test_secrets_reach_staging_but_not_cache_or_output(
    monkeypatch, minimal_config, tmp_path, capsys
):
    """The .env must still carry the real value for the server; everything
    user-visible and the local build cache must not contain it."""
    cmd, spies, printer = _secrets_setup(monkeypatch, minimal_config, tmp_path)
    cmd.force = True
    captured = {}

    def fake_rsync(_conn, staging, *_a, **_k):
        captured["env"] = (Path(staging) / ".env").read_bytes()
        return []

    monkeypatch.setattr(fujin.transfer, "rsync_to_staging", fake_rsync)
    assert invoke(cmd) == 0

    _assert_no_sentinel(printer, capsys)
    assert captured["env"] == f"STRIPE_TOKEN={SENTINEL}\nDEBUG=1".encode()
    for path in Path(spies["staging_dir"]).rglob("*"):
        if path.is_file():
            assert SENTINEL.encode() not in path.read_bytes(), path


def test_secrets_never_leak_when_build_fails(
    monkeypatch, minimal_config, tmp_path, capsys
):
    cmd, spies, printer = _secrets_setup(monkeypatch, minimal_config, tmp_path)
    monkeypatch.setattr(
        deploy_mod.build,
        "run_build",
        MagicMock(side_effect=Exception(f"build blew up with {SENTINEL} everywhere")),
    )
    with pytest.raises(DeploymentError) as excinfo:
        cmd()
    assert excinfo.value.code != 0
    _assert_no_sentinel(printer, capsys)
    assert "<$STRIPE_TOKEN>" in str(excinfo.value.message)
    # Nothing became active on the server before the failure.
    spies["rsync"].assert_not_called()
    spies["commit"].assert_not_called()
    spies["swap"].assert_not_called()


def test_secrets_never_leak_when_verify_fails(
    monkeypatch, minimal_config, tmp_path, capsys
):
    from fujin.transfer import VerifyResult

    cmd, spies, printer = _secrets_setup(monkeypatch, minimal_config, tmp_path)
    monkeypatch.setattr(
        fujin.transfer, "verify_report", lambda *a, **k: f"bad {SENTINEL}"
    )
    monkeypatch.setattr(
        fujin.transfer,
        "verify_staging",
        MagicMock(
            return_value=VerifyResult(
                ok=False, missing=[], mismatch={}, link_mismatch={}, mode_mismatch={}
            )
        ),
    )
    with pytest.raises(DeploymentError) as excinfo:
        cmd()
    _assert_no_sentinel(printer, capsys)
    assert "<$STRIPE_TOKEN>" in str(excinfo.value.message)
    spies["commit"].assert_not_called()


def test_secrets_never_leak_when_staging_commit_fails(
    monkeypatch, minimal_config, tmp_path, capsys
):
    """A failure whose own exception text contains the secret must still exit
    with a redacted message (cappa shows only the wrapped message, but the
    printed exception chain is part of the invariant surface)."""
    cmd, spies, printer = _secrets_setup(monkeypatch, minimal_config, tmp_path)
    spies["commit"].side_effect = Exception(f"fs blew up: {SENTINEL}")
    with pytest.raises(DeploymentError) as excinfo:
        cmd()
    assert SENTINEL not in str(excinfo.value.message)
    assert SENTINEL not in str(excinfo.value.__cause__)
    assert "<$STRIPE_TOKEN>" in str(excinfo.value.message)
    spies["swap"].assert_not_called()


def test_secrets_never_leak_during_auto_rollback(
    monkeypatch, minimal_config, tmp_path, capsys
):
    state = State(current="prev", previous=[])
    cmd, spies, printer = _secrets_setup(
        monkeypatch, minimal_config, tmp_path, state=state, is_failed="failed\nfailed\n"
    )
    spies["conn"].append_response(
        "/opt/fujin/testapp/releases/prev/manifest.json",
        manifest_json(version="0.9.0"),
    )
    assert invoke(cmd) == EXIT_DEPLOY_ROLLED_BACK
    _assert_no_sentinel(printer, capsys)


def test_unresolved_secret_ref_aborts_before_any_upload(
    monkeypatch, minimal_config, tmp_path, capsys
):
    """Unresolved $REF -> exit != 0 and nothing uploaded."""
    # An adapter that cannot resolve the ref returns the $-prefixed value.
    monkeypatch.setattr(deploy_mod, "resolve_secrets", lambda env, sc: env)
    minimal_config.secret_config = SecretConfig(adapter="test")
    minimal_config.hosts[0].env_content = "STRIPE_TOKEN=$STRIPE_TOKEN"
    cmd, spies = setup_deploy(
        monkeypatch, minimal_config, tmp_path, state=State(), prev_manifest=None
    )
    printer = _CapturingPrinter()
    cmd.output = printer

    code = invoke(cmd)
    assert code != 0
    spies["rsync"].assert_not_called()
    spies["commit"].assert_not_called()
    spies["swap"].assert_not_called()
    assert not spies["events"]
    joined = "\n".join(printer.lines)
    assert "<$STRIPE_TOKEN>" in joined
    out, err = capsys.readouterr()
    all_out = "\n".join([joined, out, err])
    assert "STRIPE_TOKEN=$STRIPE_TOKEN" not in all_out


def test_escaped_dollar_deploys_without_secret_adapter(
    monkeypatch, minimal_config, tmp_path
):
    """A `$$`-escaped value needs no adapter: it is unescaped into the staged
    .env instead of aborting the deploy."""
    minimal_config.secret_config = None
    minimal_config.hosts[0].env_content = "ESCAPED=$$REF\nDEBUG=1"
    cmd, _spies = setup_deploy(
        monkeypatch, minimal_config, tmp_path, state=State(), prev_manifest=None
    )
    captured = {}

    def fake_rsync(_conn, staging, *_a, **_k):
        captured["env"] = (Path(staging) / ".env").read_bytes()
        return []

    monkeypatch.setattr(fujin.transfer, "rsync_to_staging", fake_rsync)
    assert invoke(cmd) == 0
    assert captured["env"] == b'ESCAPED="$REF"\nDEBUG=1'


def test_ref_without_secret_adapter_aborts_with_adapter_less_message(
    monkeypatch, minimal_config, tmp_path
):
    """No adapter configured: fail closed, and do not blame an adapter that
    does not exist."""
    minimal_config.secret_config = None
    minimal_config.hosts[0].env_content = "STRIPE_TOKEN=$STRIPE_TOKEN"
    cmd, spies = setup_deploy(
        monkeypatch, minimal_config, tmp_path, state=State(), prev_manifest=None
    )
    printer = _CapturingPrinter()
    cmd.output = printer

    code = invoke(cmd)
    assert code != 0
    spies["rsync"].assert_not_called()
    spies["commit"].assert_not_called()
    spies["swap"].assert_not_called()
    joined = "\n".join(printer.lines)
    assert "<$STRIPE_TOKEN>" in joined
    assert "no secret adapter is configured" in joined
    assert "configured secret adapter did not resolve" not in joined


# ===========================================================================
# Identical-release reuse (#88: --force / post-rollback no commit collision)
# ===========================================================================


def _reuse_setup(
    monkeypatch,
    minimal_config,
    tmp_path,
    *,
    hooks=None,
    force=True,
    state=None,
    prev_manifest=None,
):
    """Deploy whose release dir already exists on the server."""
    monkeypatch.setattr(deploy_mod.m, "release_digest", lambda *_a, **_k: "digest-x")
    if state is None:
        state = State(current="digest-x", client_digest=None)
    cmd, spies = setup_deploy(
        monkeypatch,
        minimal_config,
        tmp_path,
        state=state,
        hooks=hooks,
        prev_manifest=prev_manifest,
    )
    cmd.force = force
    # The already-committed release dir exists.
    spies["conn"].append_response(
        "test -d /opt/fujin/testapp/releases/digest-x", ("", True)
    )
    return cmd, spies


def test_force_with_unchanged_content_reuses_committed_release(
    monkeypatch, minimal_config, tmp_path
):
    """--force with identical content must not die on commit_release's
    no-overwrite rule: the committed dir is verified and reused, the deploy
    still activates it and runs its hooks."""
    from fujin.config import HooksConfig

    hooks = HooksConfig(
        pre_install=["echo PRE"],
        post_install=["migrate-me"],
        post_start=["echo POST"],
    )
    cmd, spies = _reuse_setup(monkeypatch, minimal_config, tmp_path, hooks=hooks)
    assert invoke(cmd) == 0

    spies["commit"].assert_not_called()
    spies["rsync"].assert_called()  # upload happened before the reuse check
    spies["swap"].assert_called_once()
    assert spies["swap"].call_args.args[2] == "digest-x"
    # Hooks/restart machinery kept running on the reused release.
    assert any("echo POST" in c for c, _ in spies["conn"].calls)
    assert any("migrate-me" in c for c, _ in spies["conn"].calls)
    operations = [k.get("operation") or a[2] for a, k in spies["log_calls"]]
    assert "deploy" in operations
    written = spies["write_states"][0]
    assert written.deploys[-1].result == "ok"


def test_reuse_verifies_existing_tree_against_final_manifest(
    monkeypatch, minimal_config, tmp_path
):
    cmd, spies = _reuse_setup(monkeypatch, minimal_config, tmp_path)
    invoke(cmd)
    verify_paths = [args[0][1] for args in spies["verify"].call_args_list]
    assert verify_paths == ["/opt/fujin/testapp/releases/digest-x"], verify_paths


def test_corrupt_existing_release_replaced_by_fresh_staging(
    monkeypatch, minimal_config, tmp_path
):
    """An existing release dir that fails verification (crashed earlier
    commit) is removed after the fresh staging tree verifies, then a normal
    commit replaces it."""
    cmd, spies = _reuse_setup(monkeypatch, minimal_config, tmp_path)
    bad = fujin.transfer.VerifyResult(
        ok=False, missing=[".env"], mismatch={}, link_mismatch={}, mode_mismatch={}
    )
    spies["verify"].side_effect = [bad, fujin.transfer.VerifyResult()]
    invoke(cmd)

    spies["commit"].assert_called_once()
    rms = [
        c
        for c, kw in spies["conn"].calls
        if "rm -rf" in c and "test -d" not in c and kw.get("warn")
    ]
    assert any("releases/digest-x" in c for c in rms), rms


def test_first_deploy_after_rollback_no_commit_collision(
    monkeypatch, minimal_config, tmp_path
):
    """A stale/rolled-back state can carry a client digest that no longer
    matches, so no-op detection fires False and the deploy re-transferring
    identical content computes a final digest whose release dir already
    exists: that dir must be reused, not committed (which would refuse)."""
    # e.g. client_digest left over from before a rollback
    state = State(current="digest-x", previous=[], client_digest="legacy-client")
    monkeypatch.setattr(deploy_mod.m, "release_digest", lambda *_a, **_k: "digest-x")

    cmd, spies = setup_deploy(monkeypatch, minimal_config, tmp_path, state=state)
    spies["conn"].append_response(
        "test -d /opt/fujin/testapp/releases/digest-x", ("", True)
    )
    assert invoke(cmd) == 0

    spies["commit"].assert_not_called()
    spies["swap"].assert_called_once()
    written = spies["write_states"][0]
    assert written.deploys[-1].result == "ok"
    assert written.client_digest == "digest-x"


def test_record_rollback_does_not_poison_noop_detection():
    """Fix 2: record_rollback must not set client_digest to the final
    digest (it was never the client's pre-merge digest)."""
    from fujin import store as store_mod

    state = State(current="digest-x", previous=[])
    rolled = store_mod.record_rollback(state, "digest-x", at="t")
    assert rolled.client_digest is None


def _committed_manifest(host="prod", fingerprint="fp-prod"):
    specs = {
        p: InputFile(content=c if isinstance(c, bytes) else c.encode())
        for p, c in STAGING_FILES.items()
    }
    return build_manifest(
        app_name="testapp",
        version="0.9.0",
        config_fingerprint=fingerprint,
        dist_sha256="dist-000",
        files=specs,
        host=host,
    )


def _target_fingerprint(config):
    return deploy_mod.m.config_fingerprint(config, config.select_host(None))


def test_cross_host_guard_refuses_foreign_live_release(
    monkeypatch, minimal_config, tmp_path
):
    """Both signals differ: the live release tree belongs to another target."""
    cmd, spies = _reuse_setup(
        monkeypatch,
        minimal_config,
        tmp_path,
        force=False,
        state=State(current="digest-x", client_digest="stale-client"),
        prev_manifest=_committed_manifest(host="prod", fingerprint="fp-foreign"),
    )
    assert invoke(cmd) == EXIT_DEPLOY_FAILED
    # Refused before any upload or activation.
    spies["rsync"].assert_not_called()
    spies["verify"].assert_not_called()
    spies["swap"].assert_not_called()
    spies["commit"].assert_not_called()


def test_cross_host_guard_force_overrides(monkeypatch, minimal_config, tmp_path):
    """--force overrides the guard: the deploy proceeds with a warning."""
    cmd, spies = _reuse_setup(
        monkeypatch,
        minimal_config,
        tmp_path,
        force=True,
        prev_manifest=_committed_manifest(host="prod", fingerprint="fp-foreign"),
    )
    assert invoke(cmd) == 0
    spies["swap"].assert_called_once()


def test_host_rename_with_equal_fingerprint_is_not_blocked(
    monkeypatch, minimal_config, tmp_path
):
    """Renaming the host keeps the fingerprint: a rename is a normal deploy."""
    cmd, spies = _reuse_setup(
        monkeypatch,
        minimal_config,
        tmp_path,
        force=False,
        state=State(current="digest-x", client_digest="stale-client"),
        prev_manifest=_committed_manifest(
            host="old-name", fingerprint=_target_fingerprint(minimal_config)
        ),
    )
    assert invoke(cmd) == 0
    spies["commit"].assert_not_called()
    spies["swap"].assert_called_once()


def test_config_change_with_same_host_is_not_blocked(
    monkeypatch, minimal_config, tmp_path
):
    """A normal config change keeps the host and flips the fingerprint."""
    cmd, spies = _reuse_setup(
        monkeypatch,
        minimal_config,
        tmp_path,
        force=False,
        state=State(current="old-digest"),
        prev_manifest=_committed_manifest(host="example.com", fingerprint="fp-stale"),
    )
    assert invoke(cmd) == 0
    spies["swap"].assert_called_once()


def test_cross_host_guard_ignores_release_without_host(
    monkeypatch, minimal_config, tmp_path
):
    """A live release predating the host metadata is never a guard hit."""
    cmd, spies = _reuse_setup(
        monkeypatch,
        minimal_config,
        tmp_path,
        force=False,
        state=State(current="digest-x", client_digest="stale-client"),
        prev_manifest=_committed_manifest(host="", fingerprint="fp-foreign"),
    )
    assert invoke(cmd) == 0
    spies["swap"].assert_called_once()


def test_legitimate_config_change_not_blocked_by_state_fingerprint(
    monkeypatch, minimal_config, tmp_path
):
    """The guard compares the live release's manifest, never state.json: a
    normal config change (new fingerprint vs state) deploys fine."""
    state = State(current="old-digest", client_digest=None, config_fingerprint="old")
    monkeypatch.setattr(deploy_mod.m, "release_digest", lambda *_a, **_k: "digest-x")
    cmd, spies = setup_deploy(monkeypatch, minimal_config, tmp_path, state=state)
    assert invoke(cmd) == 0

    written = spies["write_states"][0]
    expected = deploy_mod.m.config_fingerprint(
        minimal_config, minimal_config.select_host(None)
    )
    assert written.config_fingerprint == expected != "old"


# ===========================================================================
# --full-restart on the no-op path (#79 semantics)
# ===========================================================================


def test_noop_with_full_restart_restarts_units_without_transfer(
    monkeypatch, minimal_config, tmp_path
):
    monkeypatch.setattr(deploy_mod.m, "release_digest", lambda *_a, **_k: "digest-x")
    state = State(current="prev", client_digest="digest-x")
    cmd, spies = setup_deploy(monkeypatch, minimal_config, tmp_path, state=state)
    cmd.full_restart = True
    invoke(cmd)

    spies["rsync"].assert_not_called()
    spies["commit"].assert_not_called()
    spies["swap"].assert_not_called()
    restarts = systemctl_restart_calls(spies["conn"])
    assert restarts, "--full-restart must restart units on a no-op deploy"
    assert all("systemctl restart" in c for c in restarts)


def test_noop_without_full_restart_restarts_nothing(
    monkeypatch, minimal_config, tmp_path
):
    monkeypatch.setattr(deploy_mod.m, "release_digest", lambda *_a, **_k: "digest-x")
    state = State(current="prev", client_digest="digest-x")
    cmd, spies = setup_deploy(monkeypatch, minimal_config, tmp_path, state=state)
    invoke(cmd)
    assert systemctl_restart_calls(spies["conn"]) == []


def test_oneshots_excluded_from_forced_full_restart(
    monkeypatch, minimal_config, tmp_path
):
    """#79: Type=oneshot units (migrations) are never restarted, not even
    with --full-restart."""
    oneshot_files = dict(STAGING_FILES)
    oneshot_files["systemd/testapp-migrate.service"] = (
        "[Service]\nType=oneshot\nExecStart=/bin/true\n"
    )
    cmd, spies = setup_deploy(
        monkeypatch,
        minimal_config,
        tmp_path,
        state=State(current="prev", client_digest="old"),
        prev_manifest=manifest_for(with_env=True, files=STAGING_FILES),
        files=oneshot_files,
    )
    cmd.full_restart = True
    invoke(cmd)

    joined = "\n".join(systemctl_restart_calls(spies["conn"]))
    assert "testapp-web.service" in joined
    assert "testapp-migrate.service" not in joined


# ===========================================================================
# Redaction: only actual secret refs are registered (#87)
# ===========================================================================


def test_only_secret_reference_values_are_registered_redaction(
    monkeypatch, minimal_config, tmp_path
):
    """Non-secret env values (DEBUG=..., ...) must not be registered for
    redaction: they would false-positive-replace unrelated output."""
    from fujin.config import SecretConfig

    dummy_debug = "this-is-a-long-nonsecret-debug-value"
    monkeypatch.setattr(
        deploy_mod,
        "resolve_secrets",
        lambda env, sc: env.replace("$STRIPE_TOKEN", SENTINEL),
    )
    minimal_config.secret_config = SecretConfig(adapter="test")
    minimal_config.hosts[
        0
    ].env_content = f"STRIPE_TOKEN=$STRIPE_TOKEN\nDEBUG={dummy_debug}"
    cmd, spies = setup_deploy(monkeypatch, minimal_config, tmp_path, state=State())
    printer = _CapturingPrinter()
    cmd.output = printer
    assert invoke(cmd) == 0
    # only the resolved $STRIPE_TOKEN sentinel is registered
    assert set(fujin.redaction._registered) == {SENTINEL}


def test_non_secret_env_value_never_redacted(monkeypatch, minimal_config, tmp_path):
    """A long non-secret env value must not be registered for redaction."""
    from fujin.config import SecretConfig

    plain = "zzzpliant-nonsecret-arrangement"
    monkeypatch.setattr(
        deploy_mod,
        "resolve_secrets",
        lambda env, sc: env.replace("$STRIPE_TOKEN", SENTINEL),
    )
    minimal_config.secret_config = SecretConfig(adapter="test")
    minimal_config.hosts[0].env_content = f"STRIPE_TOKEN=$STRIPE_TOKEN\nDEBUG={plain}"
    cmd, spies = setup_deploy(monkeypatch, minimal_config, tmp_path, state=State())
    printer = _CapturingPrinter()
    cmd.output = printer
    spies["commit"].side_effect = Exception(f"fs blew up: {plain}")
    with pytest.raises(DeploymentError) as excinfo:
        cmd()
    # the plain value survives; only real secrets are masked
    assert plain in str(excinfo.value.message)
    assert set(fujin.redaction._registered) == {SENTINEL}


# ===========================================================================
# Build failure surface: cappa.Exit variants keep exit codes, redacted
# ===========================================================================


def test_build_failure_as_cappa_exit_preserves_code_and_redacts(
    monkeypatch, minimal_config, tmp_path, capsys
):
    cmd, spies, printer = _secrets_setup(monkeypatch, minimal_config, tmp_path)
    monkeypatch.setattr(
        deploy_mod.build,
        "run_build",
        MagicMock(
            side_effect=cappa.Exit(f"build blew up with {SENTINEL} everywhere", code=7)
        ),
    )
    with pytest.raises(cappa.Exit) as excinfo:
        cmd()
    assert excinfo.value.code == 7
    assert SENTINEL not in str(excinfo.value.message)
    assert "<$STRIPE_TOKEN>" in str(excinfo.value.message)
    spies["rsync"].assert_not_called()


# ===========================================================================
# Deploy-time readiness tiers (#88)
# ===========================================================================

SOCKET_FILES = {
    "systemd/testapp-web.socket": ("[Socket]\nListenStream=/run/testapp/web.sock\n"),
    "bin/testapp-1.0.0-py3-none-any.whl": b"wheel",
}

SOCKET_DROPIN_FILES = {
    "systemd/testapp-web.socket": "[Socket]\n",
    "systemd/testapp-web.socket.d/listen.conf": (
        "[Socket]\nListenStream=/run/testapp/dropin.sock\n"
    ),
    "bin/testapp-1.0.0-py3-none-any.whl": b"wheel",
}

NOTIFY_FILES = {
    "systemd/testapp-web.service": ("[Service]\nType=notify\nExecStart=/bin/true\n"),
    "bin/testapp-1.0.0-py3-none-any.whl": b"wheel",
}


class _FakeClock:
    """Deterministic ``time`` replacement so poll loops never really sleep."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _probe_calls(conn, prefix):
    return [c for c, _ in conn.calls if c.startswith(prefix)]


def test_notify_service_excluded_from_is_failed_poll(
    monkeypatch, minimal_config, tmp_path
):
    """Type=notify units were validated by the blocking restart; polling them
    again with is-failed would only duplicate that verdict."""
    minimal_config.replicas = {}
    cmd, spies = setup_deploy(monkeypatch, minimal_config, tmp_path, files=NOTIFY_FILES)
    assert invoke(cmd) == 0

    assert not any("is-failed" in c for c, _ in spies["conn"].calls)


def test_notify_unit_restart_timeout_still_fails_and_rolls_back(
    monkeypatch, minimal_config, tmp_path
):
    """Acceptance: a notify unit that never sends READY=1 makes the blocking
    `systemctl restart` fail; that already routes to rollback."""
    minimal_config.replicas = {}
    state = State(current="prev", previous=[])
    cmd, spies = setup_deploy(
        monkeypatch,
        minimal_config,
        tmp_path,
        files=NOTIFY_FILES,
        state=state,
        prev_manifest=manifest_for(dist="dist-old", files=NOTIFY_FILES),
    )
    spies["conn"].append_response(
        "/opt/fujin/testapp/releases/prev/manifest.json",
        manifest_json(dist="dist-old", files=NOTIFY_FILES),
    )
    # Regression guard: the dist hash differs from the previous release, so
    # the notify unit must actually be restarted (a no restart set would let
    # this test pass without ever exercising the blocking restart).
    spies["conn"].append_response("systemctl reload-or-restart", "", False)

    assert invoke(cmd) == EXIT_DEPLOY_ROLLED_BACK

    restarts = _probe_calls(spies["conn"], "sudo systemctl reload-or-restart")
    assert restarts and "testapp-web.service" in restarts[0]

    swap_digests = [args[0][2] for args in spies["swap"].call_args_list]
    assert "prev" in swap_digests
    assert spies["write_states"][0].deploys[-1].result == "rollback"


def test_health_check_http_probe_runs(monkeypatch, minimal_config, tmp_path):
    cmd, spies = setup_deploy(
        monkeypatch, minimal_config, tmp_path, state=State(), prev_manifest=None
    )
    minimal_config.health_check = HealthCheckConfig(http="http://127.0.0.1:8000/health")

    assert invoke(cmd) == 0

    assert _probe_calls(spies["conn"], "curl") == [
        "curl -sf -o /dev/null --max-time 5 http://127.0.0.1:8000/health"
    ]


def test_health_check_per_attempt_timeout_is_capped_by_config(
    monkeypatch, minimal_config, tmp_path
):
    cmd, spies = setup_deploy(
        monkeypatch, minimal_config, tmp_path, state=State(), prev_manifest=None
    )
    minimal_config.health_check = HealthCheckConfig(
        http="http://127.0.0.1:8000/health", timeout=2
    )

    assert invoke(cmd) == 0

    assert _probe_calls(spies["conn"], "curl") == [
        "curl -sf -o /dev/null --max-time 2 http://127.0.0.1:8000/health"
    ]


def test_health_check_http_failure_rolls_back(monkeypatch, minimal_config, tmp_path):
    """Acceptance: a 500-ing app fails the probe -> journal dump -> rollback."""
    state = State(current="prev", previous=[])
    cmd, spies = setup_deploy(
        monkeypatch,
        minimal_config,
        tmp_path,
        state=state,
        prev_manifest=manifest_for(),
    )
    spies["conn"].append_response(
        "/opt/fujin/testapp/releases/prev/manifest.json",
        manifest_json(version="0.9.0"),
    )
    minimal_config.health_check = HealthCheckConfig(
        http="http://127.0.0.1:8000/health", timeout=2
    )
    spies["conn"].append_response("curl -sf", "curl: (22) 500", False)
    clock = _FakeClock()
    monkeypatch.setattr(deploy_mod, "time", clock)

    assert invoke(cmd) == EXIT_DEPLOY_ROLLED_BACK

    # Retried across the configured window (behaviour, not an exact pin).
    assert len(_probe_calls(spies["conn"], "curl -sf")) >= 2
    assert clock.now >= 2
    assert any(
        "journalctl" in c and "testapp-web.service" in c and "-n 50" in c
        for c, _ in spies["conn"].calls
    )
    swap_digests = [args[0][2] for args in spies["swap"].call_args_list]
    assert "prev" in swap_digests
    assert spies["write_states"][0].deploys[-1].result == "rollback"


def test_health_check_command_probe_is_bounded_and_shell_quoted(
    monkeypatch, minimal_config, tmp_path
):
    cmd, spies = setup_deploy(
        monkeypatch, minimal_config, tmp_path, state=State(), prev_manifest=None
    )
    minimal_config.health_check = HealthCheckConfig(
        command="test -f /opt/fujin/{app_name}/current/.env"
    )

    assert invoke(cmd) == 0

    assert _probe_calls(spies["conn"], "timeout 5 bash -lc ") == [
        "timeout 5 bash -lc 'test -f /opt/fujin/testapp/current/.env'"
    ]


def test_health_check_unresolved_placeholder_aborts_before_ssh(
    monkeypatch, minimal_config, tmp_path
):
    """Configuration errors are caught before the first SSH connection: no
    server I/O, no build, no lock."""
    cmd, spies = setup_deploy(
        monkeypatch, minimal_config, tmp_path, state=State(), prev_manifest=None
    )
    minimal_config.health_check = HealthCheckConfig(
        http="http://{nonexistent_variable}/health"
    )

    with pytest.raises(ImproperlyConfiguredError) as exc_info:
        cmd()

    assert "nonexistent_variable" in exc_info.value.message
    # Not even the SSH connection is opened: validation precedes server I/O.
    spies["conn_ctx"].assert_not_called()
    assert spies["conn"].calls == []


def test_health_check_failure_without_readiness_units_dumps_generic_journal(
    monkeypatch, minimal_config, tmp_path
):
    """Notify-only (or socket-activated) apps have no runtime service unit to
    attribute a probe failure to: dump the recent journal instead."""
    minimal_config.replicas = {}
    cmd, spies = setup_deploy(monkeypatch, minimal_config, tmp_path, files=NOTIFY_FILES)
    minimal_config.health_check = HealthCheckConfig(
        http="http://127.0.0.1:8000/health", timeout=1
    )
    spies["conn"].append_response("curl -sf", "connection refused", False)
    monkeypatch.setattr(deploy_mod, "time", _FakeClock())

    assert invoke(cmd) == EXIT_DEPLOY_FAILED

    assert any(
        c == "journalctl -n 50 --no-pager || true" for c, _ in spies["conn"].calls
    )
    assert not any(c.startswith("journalctl -u ") for c, _ in spies["conn"].calls)


def test_socket_readiness_retries_until_file_appears(
    monkeypatch, minimal_config, tmp_path
):
    minimal_config.replicas = {}
    cmd, spies = setup_deploy(monkeypatch, minimal_config, tmp_path, files=SOCKET_FILES)
    conn = spies["conn"]
    conn.append_response("systemctl is-active", "active\n")
    original_run = conn.run
    attempts = {"n": 0}

    def flaky_run(command, **kwargs):
        if command.startswith("test -S "):
            attempts["n"] += 1
            conn.calls.append((command, kwargs))
            return "", attempts["n"] > 1
        return original_run(command, **kwargs)

    conn.run = flaky_run
    monkeypatch.setattr(deploy_mod, "time", _FakeClock())

    assert invoke(cmd) == 0

    # the missing socket is retried within the poll loop, not failed at once
    assert attempts["n"] >= 2
    assert any(c == "test -S /run/testapp/web.sock" for c, _ in conn.calls)


def test_missing_socket_file_retries_then_fails_readiness(
    monkeypatch, minimal_config, tmp_path
):
    minimal_config.replicas = {}
    state = State(current="prev", previous=[])
    cmd, spies = setup_deploy(
        monkeypatch,
        minimal_config,
        tmp_path,
        files=SOCKET_FILES,
        state=state,
        prev_manifest=manifest_for(dist="dist-old", files=SOCKET_FILES),
    )
    spies["conn"].append_response(
        "/opt/fujin/testapp/releases/prev/manifest.json",
        manifest_json(dist="dist-old", files=SOCKET_FILES),
    )
    spies["conn"].append_response("test -S", "", False)
    spies["conn"].append_response("systemctl is-active", "active\n")
    clock = _FakeClock()
    monkeypatch.setattr(deploy_mod, "time", clock)

    assert invoke(cmd) == EXIT_DEPLOY_ROLLED_BACK

    checks = _probe_calls(spies["conn"], "test -S ")
    assert len(checks) >= 2
    # Exactly the command `serverops.q` renders for the ListenStream path.
    assert checks[0] == f"test -S {fujin.serverops.q('/run/testapp/web.sock')}"
    assert clock.now >= deploy_mod.READINESS_TIMEOUT
    assert any(
        "journalctl" in c and "testapp-web.socket" in c for c, _ in spies["conn"].calls
    )
    swap_digests = [args[0][2] for args in spies["swap"].call_args_list]
    assert "prev" in swap_digests
    assert spies["write_states"][0].deploys[-1].result == "rollback"


def test_socket_file_alone_is_not_ready_without_active_socket_unit(
    monkeypatch, minimal_config, tmp_path
):
    """Readiness = bound *and* connectable: an existing socket file whose
    owning socket unit is still activating is not ready."""
    minimal_config.replicas = {}
    cmd, spies = setup_deploy(monkeypatch, minimal_config, tmp_path, files=SOCKET_FILES)
    spies["conn"].append_response("test -S", "", True)
    spies["conn"].append_response("systemctl is-active", "activating\n", False)
    clock = _FakeClock()
    monkeypatch.setattr(deploy_mod, "time", clock)

    assert invoke(cmd) == EXIT_DEPLOY_FAILED

    active_calls = _probe_calls(spies["conn"], "systemctl is-active ")
    assert active_calls
    assert active_calls[0] == (
        f"systemctl is-active {fujin.serverops.q('testapp-web.socket')}"
    )
    assert clock.now >= deploy_mod.READINESS_TIMEOUT
    assert any(
        "journalctl" in c and "testapp-web.socket" in c for c, _ in spies["conn"].calls
    )


def test_socket_dropin_listen_stream_is_discovered(
    monkeypatch, minimal_config, tmp_path
):
    """A ``ListenStream=`` in ``*.socket.d/*.conf`` counts like one in the
    socket unit itself."""
    minimal_config.replicas = {}
    cmd, spies = setup_deploy(
        monkeypatch, minimal_config, tmp_path, files=SOCKET_DROPIN_FILES
    )
    spies["conn"].append_response("test -S /run/testapp/dropin.sock", "", True)
    spies["conn"].append_response("systemctl is-active", "active\n")

    assert invoke(cmd) == 0

    assert any(c == "test -S /run/testapp/dropin.sock" for c, _ in spies["conn"].calls)


def test_readiness_waits_through_activating_then_active(
    monkeypatch, minimal_config, tmp_path
):
    """A unit that reports ``activating`` is retried, not failed; once it is
    ``active`` the deploy passes."""
    cmd, spies = setup_deploy(monkeypatch, minimal_config, tmp_path, state=State())
    conn = spies["conn"]
    original_run = conn.run
    attempts = {"n": 0}

    def staged_run(command, **kwargs):
        if command.startswith("systemctl is-failed"):
            attempts["n"] += 1
            conn.calls.append((command, kwargs))
            status = "activating" if attempts["n"] == 1 else "active"
            return f"{status}\n{status}\n{status}\n", True
        return original_run(command, **kwargs)

    conn.run = staged_run
    clock = _FakeClock()
    monkeypatch.setattr(deploy_mod, "time", clock)

    assert invoke(cmd) == 0

    assert attempts["n"] >= 2
    assert clock.now >= deploy_mod.READINESS_INTERVAL


def test_unit_stuck_activating_fails_after_fallback_window(
    monkeypatch, minimal_config, tmp_path
):
    """Acceptance: a unit that never leaves ``activating`` fails the deploy
    once the explicit fallback window expires."""
    cmd, spies = setup_deploy(monkeypatch, minimal_config, tmp_path, state=State())
    conn = spies["conn"]
    original_run = conn.run
    attempts = {"n": 0}

    def stuck_run(command, **kwargs):
        if command.startswith("systemctl is-failed"):
            attempts["n"] += 1
            conn.calls.append((command, kwargs))
            return "activating\nactivating\nactivating\n", True
        return original_run(command, **kwargs)

    conn.run = stuck_run
    clock = _FakeClock()
    monkeypatch.setattr(deploy_mod, "time", clock)

    assert invoke(cmd) == EXIT_DEPLOY_FAILED

    assert attempts["n"] >= 2
    assert clock.now >= deploy_mod.READINESS_TIMEOUT
    assert any("journalctl" in c and "testapp-web.service" in c for c, _ in conn.calls)


def test_noop_full_restart_does_not_probe(monkeypatch, minimal_config, tmp_path):
    """A no-op deploy transfers nothing, so there is nothing new to probe."""
    monkeypatch.setattr(deploy_mod.m, "release_digest", lambda *_a, **_k: "digest-x")
    state = State(current="prev", client_digest="digest-x")
    cmd, spies = setup_deploy(monkeypatch, minimal_config, tmp_path, state=state)
    cmd.full_restart = True
    minimal_config.health_check = HealthCheckConfig(http="http://127.0.0.1:8000/health")

    assert invoke(cmd) == 0

    assert _probe_calls(spies["conn"], "curl") == []
    operations = [k.get("operation") or a[2] for a, k in spies["log_calls"]]
    assert "noop" in operations


# ===========================================================================
# --dry-run (#90): plan only, zero mutation
# ===========================================================================

from fujin.manifest import hash_bytes  # noqa: E402  (appended for #90 tests)

DRY_RUN_TREE = {
    "systemd/testapp-web.service": WEB_UNIT,
    "systemd/testapp-worker@.service": WEB_UNIT,
    "assets/logo.png": b"png",
    "bin/testapp-1.0.0-py3-none-any.whl": b"wheel",
}

MUTATION_TARGETS = {
    "serverops": (
        "ensure_layout",
        "ensure_app_user",
        "acquire_lock",
        "release_lock",
        "write_state",
        "install_units",
        "enable_and_start",
        "stop_and_disable",
        "link_unchanged",
        "hash_server_built",
        "install_code_into_staging",
        "normalize_staging_ownership",
        "set_envfile_mode",
        "collect_garbage",
        "install_caddyfile",
    ),
    "transfer": (
        "rsync_to_staging",
        "commit_release",
        "swap_current",
        "verify_staging",
        "cleanup_staging",
        "sweep_stale_staging",
    ),
}


def _write_cached_bundle(
    tmp_path, key="bundle-key", files=DRY_RUN_TREE, version="1.0.0"
):
    cache = tmp_path / ".fujin" / "cache" / "bundles" / key
    hashes = {}
    for path, content in files.items():
        data = content if isinstance(content, bytes) else content.encode()
        full = cache / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(data)
        hashes[path] = hash_bytes(data)
    (cache / "built.json").write_text(
        json.dumps({"bundle_key": key, "files": hashes, "version": version})
    )
    return cache


DRY_RUN_UNITS = {
    "web.service": WEB_UNIT,
    "worker@.service": WEB_UNIT,
}


def _write_local_units(config):
    systemd = config.local_config_dir / "systemd"
    systemd.mkdir(parents=True, exist_ok=True)
    for name, content in DRY_RUN_UNITS.items():
        (systemd / name).write_text(content)


def _dry_run_setup(
    monkeypatch,
    minimal_config,
    tmp_path,
    *,
    cache=False,
    state=None,
    prev_manifest=None,
    replicas=None,
    env=None,
    enabled_instances=None,
    active_instances=None,
    units=True,
    cache_version=None,
):
    """Dry-run harness: a fake read-only connection + mutation spies.

    The pre-build computation runs for real (``build.prebuild``); the
    ``units=False`` variant leaves ``.fujin/systemd`` empty to exercise the
    shared "nothing to build" guard.
    """
    if replicas is not None:
        minimal_config.replicas = replicas
    if env is not None:
        minimal_config.hosts[0].env_content = env

    if units:
        _write_local_units(minimal_config)
        pre = build_mod.prebuild(minimal_config, host=minimal_config.select_host(None))
        key = pre.bundle_key
        if cache:
            _write_cached_bundle(
                tmp_path,
                key=key,
                version=cache_version
                if cache_version is not None
                else minimal_config.local_version,
            )
    else:
        key = "bundle-key"

    # The plan must never execute the build or resolve secrets.
    monkeypatch.setattr(
        deploy_mod.build,
        "run_build",
        lambda *a, **k: pytest.fail("dry-run must not build"),
    )
    monkeypatch.setattr(
        deploy_mod,
        "resolve_secrets",
        lambda *a, **k: pytest.fail("dry-run must not resolve secrets"),
    )

    enabled = enabled_instances or []
    active = active_instances or []
    conn = FakeConn(
        responses=[
            (
                "list-unit-files",
                (
                    "".join(f"{name} enabled\n" for name in enabled),
                    True,
                ),
            ),
            (
                "list-units",
                (
                    "".join(f"{name} loaded active running\n" for name in active),
                    True,
                ),
            ),
        ]
    )
    conn_ctx = MagicMock()
    conn_ctx.return_value.__enter__.return_value = conn
    conn_ctx.return_value.__exit__.return_value = None
    monkeypatch.setattr(fujin.connection, "connection", conn_ctx)

    monkeypatch.setattr(fujin.serverops, "read_state", lambda _c, _a: state)
    monkeypatch.setattr(
        fujin.serverops, "read_current_manifest", lambda _c, _a: prev_manifest
    )

    spies = {}
    for module, names in MUTATION_TARGETS.items():
        target = getattr(deploy_mod, module)
        for name in names:
            spy = MagicMock()
            monkeypatch.setattr(target, name, spy)
            spies[name] = spy
    log = MagicMock()
    monkeypatch.setattr(deploy_mod, "log_operation", log)
    spies["log_operation"] = log

    cmd = Deploy(no_input=True)
    cmd.dry_run = True
    cmd.config = minimal_config
    printer = _CapturingPrinter()
    cmd.output = printer
    return cmd, printer, conn, spies


def _assert_no_mutations(spies, conn):
    for name, spy in spies.items():
        assert not spy.called, f"{name} must not run during --dry-run"
    assert conn.uploads == []
    for command, _kw in conn.calls:
        assert command.startswith(
            (
                "readlink ",
                "systemctl list-unit-files",
                "systemctl list-units",
                # Read-only: enabled template instances live as ``.wants``
                # symlinks, which list-unit-files never reports.
                "find /etc/systemd/system",
            )
        ), command


def test_dry_run_fresh_host_prints_first_deploy_plan(
    monkeypatch, minimal_config, tmp_path
):
    cmd, printer, conn, spies = _dry_run_setup(
        monkeypatch, minimal_config, tmp_path, replicas={"worker": 2}
    )

    assert invoke(cmd) == 0

    lines = "\n".join(printer.lines)
    assert "host: example.com (example.com)" in lines
    assert "installation_mode: python-package" in lines
    assert "app_user: testapp" in lines
    assert "bundle_key: " in lines
    assert "cache: would run: echo building" in lines
    assert "swap: unknown until build" in lines
    assert "live_release: none" in lines
    assert "change_expected: yes (first deploy)" in lines
    assert "source_entries unit: systemd/testapp-web.service" in lines
    assert "note: the dist/code entry is unknown until the build runs." in lines
    assert "restart: unknown until build" in lines
    # A fresh host has no instances: every configured replica starts.
    assert "converge_start: testapp-worker@1.service, testapp-worker@2.service" in lines
    assert "converge_stop: none" in lines
    assert "hooks_pre_install: none" in lines
    assert "hooks_post_install: none" in lines
    assert "hooks_post_start: none" in lines
    _assert_no_mutations(spies, conn)


def test_dry_run_cache_hit_uses_cached_manifest(monkeypatch, minimal_config, tmp_path):
    prev = manifest_for(files=DRY_RUN_TREE, dist="older-dist")
    cmd, printer, conn, spies = _dry_run_setup(
        monkeypatch,
        minimal_config,
        tmp_path,
        cache=True,
        prev_manifest=prev,
        replicas={"worker": 2},
    )
    # Entries are compared against the cached bundle, not the live tree.
    assert invoke(cmd) == 0

    lines = "\n".join(printer.lines)
    assert "cache: cached (" in lines
    assert "cache: would run:" not in lines
    # dist differs -> code change restarts every app service unit.
    assert "restart: testapp-web.service, testapp-worker@.service" in lines
    _assert_no_mutations(spies, conn)


def test_dry_run_prints_entries_grouped_by_kind(monkeypatch, minimal_config, tmp_path):
    prev_files = {
        path: content
        for path, content in DRY_RUN_TREE.items()
        if path != "assets/logo.png"
    }
    prev_files["assets/old.png"] = b"old"
    prev = manifest_for(files=prev_files, dist="older-dist")
    cmd, printer, _conn, _spies = _dry_run_setup(
        monkeypatch,
        minimal_config,
        tmp_path,
        cache=True,
        prev_manifest=prev,
    )

    assert invoke(cmd) == 0

    lines = "\n".join(printer.lines)
    assert (
        "entries asset: added=assets/logo.png changed=- removed=assets/old.png" in lines
    )
    # Unchanged unit/code entries produce no line at all.
    assert "entries unit:" not in lines
    assert "entries code:" not in lines


def test_dry_run_change_expected_no_when_plan_matches_live(
    monkeypatch, minimal_config, tmp_path
):
    monkeypatch.setattr(deploy_mod.m, "config_fingerprint", lambda *_a, **_k: "fp0")
    # Match the cached manifest exactly: the dist entry hash comes from the
    # cached wheel, and the fingerprint is pinned above (before the bundle
    # key is computed, so the cache lookup uses the pinned value).
    prev = manifest_for(
        version=minimal_config.local_version,
        files=DRY_RUN_TREE,
        dist=hash_bytes(b"wheel"),
    )
    cmd, printer, _conn, _spies = _dry_run_setup(
        monkeypatch,
        minimal_config,
        tmp_path,
        cache=True,
        prev_manifest=prev,
        state=State(current="a" * 64),
    )

    assert invoke(cmd) == 0

    lines = "\n".join(printer.lines)
    assert f"live_release: {'a' * 64}" in lines
    assert "config_fingerprint: fp0 (live: fp0)" in lines
    assert "env: unchanged" in lines
    assert "change_expected: no" in lines
    assert "client_digest: " in lines
    assert "swap: releases/" in lines
    assert "transfer: 0 files (4 unchanged hardlinked from the live release)" in lines
    assert "entries: no changes" in lines
    assert "restart: none" in lines


def test_dry_run_change_expected_yes_on_fingerprint_drift(
    monkeypatch, minimal_config, tmp_path
):
    prev = manifest_for(files=DRY_RUN_TREE, dist="dist-000")
    cmd, printer, _conn, _spies = _dry_run_setup(
        monkeypatch,
        minimal_config,
        tmp_path,
        cache=True,
        prev_manifest=prev,
        state=State(current="b" * 64),
    )

    assert invoke(cmd) == 0

    lines = "\n".join(printer.lines)
    assert "change_expected: yes" in lines


def test_dry_run_convergence_prints_start_and_stop(
    monkeypatch, minimal_config, tmp_path
):
    cmd, printer, _conn, _spies = _dry_run_setup(
        monkeypatch,
        minimal_config,
        tmp_path,
        cache=True,
        replicas={"worker": 2},
        enabled_instances=[
            "testapp-worker@1.service",
            "testapp-worker@3.service",
        ],
        active_instances=[
            "testapp-worker@1.service",
            "testapp-worker@3.service",
        ],
    )

    assert invoke(cmd) == 0

    lines = "\n".join(printer.lines)
    assert "converge_start: testapp-worker@2.service" in lines
    assert "converge_stop: testapp-worker@3.service" in lines


def test_dry_run_prints_hook_lists(monkeypatch, minimal_config, tmp_path):
    from fujin.config import HooksConfig

    cmd, printer, _conn, _spies = _dry_run_setup(monkeypatch, minimal_config, tmp_path)
    minimal_config.hooks_config = HooksConfig(
        pre_install=["echo pre"],
        post_install=["echo post"],
        post_start=["echo start"],
    )

    assert invoke(cmd) == 0

    lines = "\n".join(printer.lines)
    assert "hooks_pre_install: echo pre" in lines
    assert "hooks_post_install: echo post" in lines
    assert "hooks_post_start: echo start" in lines


def test_dry_run_renders_secret_refs_without_values(
    monkeypatch, minimal_config, tmp_path
):
    cmd, printer, _conn, _spies = _dry_run_setup(
        monkeypatch,
        minimal_config,
        tmp_path,
        env="STRIPE_TOKEN=$STRIPE_TOKEN\nDEBUG=1",
    )

    assert invoke(cmd) == 0

    lines = "\n".join(printer.lines)
    assert "env_refs: STRIPE_TOKEN=<$STRIPE_TOKEN>" in lines
    assert SENTINEL not in lines
    assert "$STRIPE_TOKEN\n" not in lines


def test_two_consecutive_dry_runs_print_same_plan(
    monkeypatch, minimal_config, tmp_path
):
    cmd1, printer1, _conn1, _spies1 = _dry_run_setup(
        monkeypatch, minimal_config, tmp_path, cache=True
    )
    assert invoke(cmd1) == 0

    cmd2, printer2, _conn2, _spies2 = _dry_run_setup(
        monkeypatch, minimal_config, tmp_path, cache=True
    )
    assert invoke(cmd2) == 0

    assert printer1.lines == printer2.lines


def test_dry_run_no_cache_ignores_existing_cache(monkeypatch, minimal_config, tmp_path):
    cmd, printer, _conn, _spies = _dry_run_setup(
        monkeypatch, minimal_config, tmp_path, cache=True
    )
    cmd.no_cache = True

    assert invoke(cmd) == 0

    assert "cache: would run: echo building" in "\n".join(printer.lines)


def test_dry_run_ignores_server_built_venv_entries(
    monkeypatch, minimal_config, tmp_path
):
    """venv/** exists only on the server: it must not look like a removal."""
    monkeypatch.setattr(deploy_mod.m, "config_fingerprint", lambda *_a, **_k: "fp0")
    live_files = dict(DRY_RUN_TREE)
    live_files["venv/lib/python3.11/site-packages/pkg/__init__.py"] = b"x"
    prev = manifest_for(
        version=minimal_config.local_version,
        files=live_files,
        dist=hash_bytes(b"wheel"),
    )
    cmd, printer, _conn, _spies = _dry_run_setup(
        monkeypatch,
        minimal_config,
        tmp_path,
        cache=True,
        prev_manifest=prev,
        state=State(current="d" * 64),
    )

    assert invoke(cmd) == 0

    lines = "\n".join(printer.lines)
    assert "venv/" not in lines
    assert "change_expected: no" in lines
    assert "entries: no changes" in lines
    assert "restart: none" in lines
    assert "transfer: 0 files" in lines


def test_dry_run_reports_version_drift_of_cached_bundle(
    monkeypatch, minimal_config, tmp_path
):
    monkeypatch.setattr(deploy_mod.m, "config_fingerprint", lambda *_a, **_k: "fp0")
    prev = manifest_for(
        version=minimal_config.local_version,
        files=DRY_RUN_TREE,
        dist=hash_bytes(b"wheel"),
    )
    cmd, printer, _conn, _spies = _dry_run_setup(
        monkeypatch,
        minimal_config,
        tmp_path,
        cache=True,
        cache_version="0.9.0",
        prev_manifest=prev,
        state=State(current="e" * 64),
    )

    assert invoke(cmd) == 0

    lines = "\n".join(printer.lines)
    assert "entries: no changes" in lines
    assert "change_expected: yes" in lines


def test_dry_run_reflects_force_and_full_restart_flags(
    monkeypatch, minimal_config, tmp_path
):
    cmd, printer, _conn, _spies = _dry_run_setup(
        monkeypatch, minimal_config, tmp_path, cache=True
    )
    cmd.force = True
    cmd.full_restart = True

    assert invoke(cmd) == 0

    lines = "\n".join(printer.lines)
    assert "change_expected: yes (--force)" in lines
    assert "restart: all units (--full-restart)" in lines


def test_dry_run_without_cache_prints_unknown_swap_and_restart(
    monkeypatch, minimal_config, tmp_path
):
    cmd, printer, _conn, _spies = _dry_run_setup(monkeypatch, minimal_config, tmp_path)

    assert invoke(cmd) == 0

    lines = "\n".join(printer.lines)
    assert "swap: unknown until build" in lines
    assert "restart: unknown until build" in lines


def test_dry_run_prints_transfer_and_env_for_cache_hit(
    monkeypatch, minimal_config, tmp_path
):
    prev = manifest_for(files=DRY_RUN_TREE, dist="older-dist")
    cmd, printer, _conn, _spies = _dry_run_setup(
        monkeypatch,
        minimal_config,
        tmp_path,
        cache=True,
        prev_manifest=prev,
        state=State(current="f" * 64),
    )

    assert invoke(cmd) == 0

    lines = "\n".join(printer.lines)
    assert "client_digest: " in lines
    assert "swap: releases/" in lines
    assert "transfer: 0 files (4 unchanged hardlinked from the live release)" in lines
    assert "env: changed" in lines


def test_dry_run_does_not_write_the_build_cache(monkeypatch, minimal_config, tmp_path):
    cmd, _printer, _conn, _spies = _dry_run_setup(
        monkeypatch, minimal_config, tmp_path, cache=True
    )
    cache_root = tmp_path / ".fujin" / "cache"

    def snapshot():
        return {
            str(path): path.stat().st_mtime_ns
            for path in (cache_root, *cache_root.rglob("*"))
        }

    before = snapshot()
    assert invoke(cmd) == 0
    assert snapshot() == before


def test_dry_run_and_build_share_the_prebuild_computation(
    monkeypatch, minimal_config, tmp_path
):
    cmd, printer, _conn, _spies = _dry_run_setup(monkeypatch, minimal_config, tmp_path)

    assert invoke(cmd) == 0

    expected = build_mod.prebuild(minimal_config).bundle_key
    assert f"bundle_key: {expected}" in "\n".join(printer.lines)


def test_dry_run_empty_systemd_dir_fails_like_a_real_build(
    monkeypatch, minimal_config, tmp_path
):
    cmd, printer, conn, _spies = _dry_run_setup(
        monkeypatch, minimal_config, tmp_path, units=False
    )

    with pytest.raises(DeploymentError) as dry:
        cmd()
    assert dry.value.message == "No systemd units found, nothing to build"

    with pytest.raises(DeploymentError) as real:
        build_mod.prebuild(minimal_config)
    assert real.value.message == dry.value.message
    assert real.value.code == dry.value.code
    # Nothing was printed and no SSH connection was opened.
    assert printer.lines == []
    assert conn.calls == []


def test_dry_run_rejects_unresolved_env_placeholder_before_output(
    monkeypatch, minimal_config, tmp_path
):
    cmd, printer, conn, _spies = _dry_run_setup(
        monkeypatch,
        minimal_config,
        tmp_path,
        env="FOO={missing}",
    )

    with pytest.raises(DeploymentError) as exc:
        cmd()

    assert "missing" in exc.value.message
    assert printer.lines == []
    assert conn.calls == []


def test_dry_run_validates_health_check_probe_before_output(
    monkeypatch, minimal_config, tmp_path
):
    cmd, printer, conn, _spies = _dry_run_setup(
        monkeypatch, minimal_config, tmp_path, units=False
    )
    minimal_config.health_check = HealthCheckConfig(command="curl {missing}")

    with pytest.raises(ImproperlyConfiguredError) as exc:
        cmd()

    assert "missing" in str(exc.value.message)
    assert printer.lines == []
    assert conn.calls == []


# ===========================================================================
# Short read-only probes (#transport robustness)
# ===========================================================================


def test_probe_retries_once_on_transport_stall():
    """A wedged exec channel on an idempotent probe is retried, not fatal."""
    conn = MagicMock()
    conn.run.side_effect = [ConnectionError("channel wedged"), ("digest", True)]

    assert deploy_mod._probe(conn, "readlink /x") == ("digest", True)
    assert conn.run.call_count == 2
    assert conn.run.call_args.kwargs["idle_timeout"] == 15


def test_probe_reraises_after_last_attempt():
    conn = MagicMock()
    conn.run.side_effect = ConnectionError("channel wedged")

    with pytest.raises(ConnectionError):
        deploy_mod._probe(conn, "readlink /x")

    assert conn.run.call_count == 2
