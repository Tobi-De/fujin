"""Tests for the release-tree deploy command (wave 3b, slices #76-#79).

No-op short-circuit, --force bypass, diff-based restart-set wiring,
replica convergence, state/GC updates, hook ordering and auto-rollback
semantics - all against a fake connection, no live SSH.

Tests pinning the retired zipapp/installer bundle flow were deleted with
that flow.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import fujin.commands.deploy as deploy_mod
import fujin.connection
import fujin.serverops
import fujin.transfer
from fujin.build import BuildResult
from fujin.commands.deploy import Deploy
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

    def run(self, cmd, **kw):
        self.calls.append((cmd, kw))
        if self.events is not None:
            self.events.append(("conn", cmd))
        for marker, (out, ok) in self.responses:
            if marker in cmd:
                return out, ok
        return "", True

    def append_response(self, marker, out, ok=True):
        self.responses.append((marker, (out, ok)))


def invoke(cmd) -> int:
    """Run a deploy; return its exit code (0 when it completed normally)."""
    try:
        cmd()
        return 0
    except SystemExit as exc:
        code = exc.code
        return 0 if code is None else code


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
    spies["conn"].append_response(
        "list-unit-files", "testapp-worker@1.service template"
    )
    cmd()

    started = [str(c) for c in spies["enable_and_start"].call_args_list]
    assert any("testapp-worker@2.service" in u for u in started)
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
# Start failure / auto rollback (existing exit-3 contract)
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
        f"cat /opt/fujin/testapp/releases/{prev_digest}/manifest.json",
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
    assert invoke(cmd) == 3

    swap_digests = [args[0][2] for args in spies["swap"].call_args_list]
    assert "prev" in swap_digests
    written = spies["write_states"][0]
    assert written.current == "prev"
    assert written.deploys[-1].result == "rollback"


def test_no_rollback_exits_3_without_swapping(monkeypatch, minimal_config, tmp_path):
    cmd, spies = _setup_failing_deploy(
        monkeypatch, minimal_config, tmp_path, no_rollback=True
    )
    assert invoke(cmd) == 3
    # The new release was activated (swapped in); --no-rollback must NOT
    # swap back to the previous digest.
    rollback_swaps = [
        a for args, _k in spies["swap"].call_args_list for a in args if a == "prev"
    ]
    assert rollback_swaps == []


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


def test_scp_fallback_stages_env_and_skips_bookkeeping(
    monkeypatch, minimal_config, tmp_path
):
    """SCP-fallback path: the transfer stages the whole on-disk tree, so the
    local cache bookkeeping file (built.json) must be excluded explicitly;
    the rendered .env rides along with the tree."""
    from fujin.transfer import _scp_fallback

    minimal_config.hosts[0].env_content = "SECRET_TOKEN=1\n"
    cmd, spies = setup_deploy(
        monkeypatch, minimal_config, tmp_path, state=State(), prev_manifest=None
    )
    # Reproduce the staging tree the deploy code writes (with .env bytes and
    # with built.json present in the cache dir, as run_build leaves it).
    staging = spies["staging_dir"]
    (staging / ".env").write_bytes(b"SECRET_TOKEN=1\n")
    (staging / "built.json").write_bytes(b'{"bundle_key": "k"}')

    conn = FakeConn()

    def run(cmd, **kw):
        conn.calls.append((cmd, kw))
        return "", True

    conn.run = run
    puts = []
    conn.put = lambda src, dest: puts.append(dest)

    _scp_fallback(conn, staging, "/remote/.staging-1", modes={}, exclude={"built.json"})

    uploaded = {p.rsplit("/", 1)[1] for p in puts}
    assert "built.json" not in uploaded
    assert uploaded == {
        "testapp-1.0.0-py3-none-any.whl",
        "logo.png",
        "testapp-web.service",
        "testapp-worker@.service",
        ".env",
    }


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
        "cat /opt/fujin/testapp/releases/third/manifest.json",
        manifest_json(version="1.0.0"),
    )
    assert invoke(cmd) == 3

    swap_digests = [args[0][2] for args in spies["swap"].call_args_list]
    assert "third" in swap_digests
    assert "second" not in swap_digests
    written = spies["write_states"][0]
    assert written.current == "third"
