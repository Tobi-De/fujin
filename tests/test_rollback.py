"""Tests for the release-tree rollback command (wave 3a, slice #80).

Error handling, release selection logic, single-level failure semantics and
pure state updates - all without live SSH integration.

Tests pinning the obsolete ``.versions/*.pyz`` uninstall/install flow were
deleted along with that flow.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

import cappa

import fujin.commands.rollback as rollback
import fujin.connection
import fujin.serverops
import fujin.transfer
from fujin.commands.rollback import Rollback
from fujin.exit_codes import EXIT_OK, EXIT_ROLLBACK_FAILED, EXIT_USER_ABORTED
from fujin.manifest import build_manifest, manifest_to_dict
from fujin.store import DeployRecord, State

# ===========================================================================
# Helpers
# ===========================================================================

WEB_UNIT = "[Service]\nType=simple\n"
WORKER_TEMPLATE_UNIT = "[Service]\nType=simple\n"
ONESHOT_UNIT = "[Service]\nType=oneshot\n"


def default_files(extra=None):
    files = {
        "systemd/testapp-web.service": WEB_UNIT,
        "systemd/testapp-worker@.service": WORKER_TEMPLATE_UNIT,
        "systemd/common.d/hardening.conf": "[Service]\nLimitNOFILE=65536",
        "assets/static/logo.png": b"png",
    }
    if extra:
        files.update(extra)
    return files


def make_manifest(version, dist, *, files=None):
    if files is None:
        files = default_files()
    return build_manifest(
        app_name="testapp",
        version=version,
        config_fingerprint="fp",
        dist_sha256=dist,
        files=files,
    )


class FakeConn:
    def __init__(self, responses=None):
        self.calls = []
        self.gets: list[tuple[str, str]] = []
        self.responses = responses or []

    def run(self, cmd, **kw):
        self.calls.append(cmd)
        for marker, (out, ok) in self.responses:
            if marker in cmd:
                return out, ok
        # Existence checks follow the manifest marker registry: a test
        # -d/-f target exists iff some marker is that path or inside it.
        # A blind ok=True default would break digest-vs-version targeting;
        # a blind False would break the layout gate.
        if cmd.startswith("test -d ") or cmd.startswith("test -f "):
            target = cmd.split(" ", 2)[2].split()[0].strip("'\"")
            return "", bool(
                any(
                    m == target or m.startswith(target + "/") for m, _ in self.responses
                )
            )
        return "", True

    def get(self, remote, local, verify=False):
        """Fake SCP download: answers from the same marker registry."""
        self.gets.append((remote, local))
        for marker, (out, ok) in self.responses:
            if marker in remote:
                if not ok:
                    raise FileNotFoundError(remote)
                with open(local, "w") as fh:
                    fh.write(out)
                return
        raise FileNotFoundError(remote)


def manifest_response(digest, version, dist, files=None):
    data = json.dumps(manifest_to_dict(make_manifest(version, dist, files=files)))
    return (f"/opt/fujin/testapp/releases/{digest}/manifest.json", (data, True))


def unit_response(digest, unit_name, content):
    return (
        f"/opt/fujin/testapp/releases/{digest}/{unit_name}",
        (content, True),
    )


def unit_responses_for(digest, files):
    return [
        unit_response(digest, unit_name, content)
        for unit_name, content in sorted(files.items())
        if unit_name.startswith("systemd/")
    ]


MANIFEST_RESPONSES = [
    manifest_response("a-digest", "1.0.0", "dist-aaa"),
    manifest_response("b-digest", "1.0.1", "dist-bbb"),
    manifest_response("c-digest", "1.0.2", "dist-ccc"),
]

LAYOUT_RESPONSES = [
    ("test -d /opt/fujin/testapp/store", ("", True)),
    ("test -d /opt/fujin/testapp/.install", ("", False)),
]

DEFAULT_CONN_RESPONSES = [
    *LAYOUT_RESPONSES,
    ("ls -1t releases/", ("b-digest\na-digest\nc-digest\n", True)),
    *MANIFEST_RESPONSES,
    *unit_responses_for("a-digest", default_files()),
]


def setup_rollback(
    monkeypatch,
    minimal_config,
    conn,
    *,
    state,
    current_manifest,
    verify_ok=True,
    to=None,
    previous=False,
    strict=False,
    prompt_answer=1,
):
    """Assemble a Rollback command with a mocked connection and modules."""
    monkeypatch.setattr(fujin.serverops, "acquire_lock", lambda *a, **k: "flock:1")
    lock_released = []
    monkeypatch.setattr(
        fujin.serverops,
        "release_lock",
        lambda _c, _a, _t: lock_released.append(_t),
    )
    monkeypatch.setattr(fujin.serverops, "read_state", lambda _c, _a: state)
    monkeypatch.setattr(
        fujin.serverops, "read_current_manifest", lambda _c, _a: current_manifest
    )
    install_units = MagicMock()
    monkeypatch.setattr(fujin.serverops, "install_units", install_units)
    stop_and_disable = MagicMock(return_value=True)
    monkeypatch.setattr(fujin.serverops, "stop_and_disable", stop_and_disable)
    enable_and_start = MagicMock(return_value=True)
    monkeypatch.setattr(fujin.serverops, "enable_and_start", enable_and_start)
    monkeypatch.setattr(
        fujin.serverops, "install_caddyfile", MagicMock(return_value=True)
    )
    write_state = MagicMock()
    monkeypatch.setattr(fujin.serverops, "write_state", write_state)

    swap_current = MagicMock()
    monkeypatch.setattr(fujin.transfer, "swap_current", swap_current)
    verify = MagicMock(
        return_value=fujin.transfer.VerifyResult(
            ok=verify_ok, missing=[], mismatch={}, link_mismatch={}, mode_mismatch={}
        )
    )
    monkeypatch.setattr(fujin.transfer, "verify_staging", verify)

    probe = MagicMock()
    if prompt_answer is not None:
        probe.ask.return_value = prompt_answer
    monkeypatch.setattr(rollback, "IntPrompt", probe)

    ctx = MagicMock()
    ctx.return_value.__enter__.return_value = conn
    monkeypatch.setattr(fujin.connection, "connection", ctx)

    cmd = Rollback(previous=previous, to=to, strict=strict)
    cmd.config = minimal_config
    cmd.output = MagicMock()
    return cmd, write_state, swap_current, verify, install_units


def exit_code_of(callable_):
    try:
        callable_()
    except SystemExit as exc:
        return exc.code
    raise AssertionError("expected SystemExit")


# ===========================================================================
# Defaults / state transitions
# ===========================================================================


def test_default_rolls_back_to_previous_zero(monkeypatch, minimal_config):
    conn = FakeConn(DEFAULT_CONN_RESPONSES)
    state = State(
        current="b-digest",
        previous=["a-digest"],
        deploys=[
            DeployRecord(manifest="a-digest", at="t0", result="ok"),
            DeployRecord(manifest="b-digest", at="t1", result="ok"),
        ],
    )
    current = make_manifest("1.0.1", "dist-bbb")
    cmd, write_state, swap_current, _verify, install_units = setup_rollback(
        monkeypatch, minimal_config, conn, state=state, current_manifest=current
    )

    result = cmd()

    assert result is None
    swap_current.assert_called_once_with(conn, "/opt/fujin/testapp", "a-digest")
    new_state = write_state.call_args.args[-1]
    assert new_state.current == "a-digest"
    assert new_state.previous == ["b-digest"]
    assert new_state.deploys[-1].result == "rollback"
    assert write_state.call_args.args[1] == "/opt/fujin/testapp"
    unit_files = install_units.call_args.args[2]
    assert set(unit_files) == {"testapp-web.service", "testapp-worker@.service"}


def test_state_update_semantics_after_rollback(monkeypatch, minimal_config):
    conn = FakeConn(DEFAULT_CONN_RESPONSES)
    state = State(current="b-digest", previous=["a-digest"])
    current = make_manifest("1.0.1", "dist-bbb")
    cmd, write_state, _swap, _v, _iu = setup_rollback(
        monkeypatch, minimal_config, conn, state=state, current_manifest=current
    )
    cmd()
    new_state = write_state.call_args.args[-1]
    assert new_state.current == "a-digest"
    assert new_state.previous == ["b-digest"]
    # client_digest must NOT be set to the final digest: the target was
    # never transferred by the client, and recording it there would
    # permanently break #78 no-op detection.
    assert new_state.client_digest is None
    assert new_state.units_installed_from == "a-digest"
    assert new_state.deploys[-1].manifest == "a-digest"


def test_previous_flag_missing_previous_errors(monkeypatch, minimal_config):
    conn = FakeConn(DEFAULT_CONN_RESPONSES)
    state = State(current="b-digest", previous=[])
    current = make_manifest("1.0.1", "dist-bbb")
    cmd, *_ = setup_rollback(
        monkeypatch,
        minimal_config,
        conn,
        state=state,
        current_manifest=current,
        previous=True,
    )
    assert exit_code_of(lambda: cmd()) == EXIT_USER_ABORTED


# ===========================================================================
# Explicit --to target
# ===========================================================================


def test_to_digest_selects_target_release(monkeypatch, minimal_config):
    conn = FakeConn(DEFAULT_CONN_RESPONSES)
    state = State(current="b-digest", previous=["a-digest"])
    current = make_manifest("1.0.1", "dist-bbb")
    cmd, _ws, swap_current, _v, _iu = setup_rollback(
        monkeypatch,
        minimal_config,
        conn,
        state=state,
        current_manifest=current,
        to="a-digest",
    )
    cmd()
    swap_current.assert_called_once_with(conn, "/opt/fujin/testapp", "a-digest")


def test_to_sha256_prefixed_digest_normalizes(monkeypatch, minimal_config):
    conn = FakeConn(DEFAULT_CONN_RESPONSES)
    state = State(current="b-digest", previous=["a-digest"])
    current = make_manifest("1.0.1", "dist-bbb")
    cmd, _ws, swap_current, _v, _iu = setup_rollback(
        monkeypatch,
        minimal_config,
        conn,
        state=state,
        current_manifest=current,
        to="sha256:a-digest",
    )
    cmd()
    assert any("releases/" in c and "a-digest" in c for c in conn.calls)
    swap_current.assert_called_once()


def test_to_digest_gc_release_hard_error(monkeypatch, minimal_config):
    conn = FakeConn(DEFAULT_CONN_RESPONSES)
    state = State(
        current="b-digest",
        previous=["a-digest"],
        deploys=[
            DeployRecord(manifest="c-digest", at="t0", result="ok"),
            DeployRecord(manifest="a-digest", at="t1", result="ok"),
            DeployRecord(manifest="b-digest", at="t2", result="ok"),
        ],
    )
    current = make_manifest("1.0.1", "dist-bbb")
    cmd, _ws, swap_current, _v, _iu = setup_rollback(
        monkeypatch,
        minimal_config,
        conn,
        state=state,
        current_manifest=current,
        to="zz-pruned",
    )
    assert exit_code_of(lambda: cmd()) == EXIT_USER_ABORTED
    swap_current.assert_not_called()


def test_to_current_digest_is_noop(monkeypatch, minimal_config):
    conn = FakeConn(DEFAULT_CONN_RESPONSES)
    state = State(current="b-digest", previous=["a-digest"])
    current = make_manifest("1.0.1", "dist-bbb")
    cmd, _ws, swap_current, _v, _iu = setup_rollback(
        monkeypatch,
        minimal_config,
        conn,
        state=state,
        current_manifest=current,
        to="b-digest",
    )
    assert exit_code_of(lambda: cmd()) == EXIT_OK
    swap_current.assert_not_called()


# ===========================================================================
# Interactive prompt behaviour
# ===========================================================================


def test_interactive_prompt_lists_kept_releases_only(monkeypatch, minimal_config):
    conn = FakeConn(DEFAULT_CONN_RESPONSES)
    state = State(
        current="b-digest",
        previous=["a-digest"],
        deploys=[
            DeployRecord(manifest="c-digest", at="t0", result="ok"),
            DeployRecord(manifest="a-digest", at="t1", result="ok"),
            DeployRecord(manifest="b-digest", at="t2", result="ok"),
        ],
    )
    current = make_manifest("1.0.1", "dist-bbb")
    cmd, _ws, _sw, _v, _iu = setup_rollback(
        monkeypatch,
        minimal_config,
        conn,
        state=state,
        current_manifest=current,
        prompt_answer=1,
    )
    cmd()
    assert rollback.IntPrompt.ask.called


def test_no_targets_info_message_non_strict(monkeypatch, minimal_config):
    conn = FakeConn(
        [
            *LAYOUT_RESPONSES,
            ("ls -1t releases/", ("b-digest\n", True)),
            *MANIFEST_RESPONSES,
        ]
    )
    state = State(current="b-digest", previous=[])
    current = make_manifest("1.0.1", "dist-bbb")
    cmd, *_ = setup_rollback(
        monkeypatch,
        minimal_config,
        conn,
        state=state,
        current_manifest=current,
        strict=False,
    )
    assert exit_code_of(lambda: cmd()) == EXIT_OK


def test_strict_no_targets_errors(monkeypatch, minimal_config):
    conn = FakeConn(
        [
            *LAYOUT_RESPONSES,
            ("ls -1t releases/", ("b-digest\n", True)),
            *MANIFEST_RESPONSES,
        ]
    )
    state = State(current="b-digest", previous=[])
    current = make_manifest("1.0.1", "dist-bbb")
    cmd, *_ = setup_rollback(
        monkeypatch,
        minimal_config,
        conn,
        state=state,
        current_manifest=current,
        strict=True,
    )
    assert exit_code_of(lambda: cmd()) == EXIT_USER_ABORTED


# ===========================================================================
# Old-layout detection / no store
# ===========================================================================


def test_old_layout_without_store_dir_errors(monkeypatch, minimal_config):
    conn = FakeConn([("test -d /opt/fujin/testapp/store", ("", False))])
    cmd, *_ = setup_rollback(
        monkeypatch, minimal_config, conn, state=None, current_manifest=None
    )
    assert exit_code_of(lambda: cmd()) == EXIT_USER_ABORTED


def test_missing_state_errors(monkeypatch, minimal_config):
    conn = FakeConn(DEFAULT_CONN_RESPONSES)
    cmd, *_ = setup_rollback(
        monkeypatch, minimal_config, conn, state=None, current_manifest=None
    )
    assert exit_code_of(lambda: cmd()) == EXIT_USER_ABORTED


# ===========================================================================
# Verification is mandatory before the swap
# ===========================================================================


def test_verify_failure_aborts_before_swap(monkeypatch, minimal_config):
    conn = FakeConn(DEFAULT_CONN_RESPONSES)
    state = State(current="b-digest", previous=["a-digest"])
    current = make_manifest("1.0.1", "dist-bbb")
    cmd, write_state, swap_current, _v, _iu = setup_rollback(
        monkeypatch,
        minimal_config,
        conn,
        state=state,
        current_manifest=current,
        verify_ok=False,
    )
    assert exit_code_of(lambda: cmd()) == EXIT_USER_ABORTED
    swap_current.assert_not_called()
    write_state.assert_not_called()


# ===========================================================================
# Single level: restart failure does NOT recurse
# ===========================================================================


def test_restart_failure_is_loud_and_no_recursion(monkeypatch, minimal_config):
    conn = FakeConn(
        [
            *LAYOUT_RESPONSES,
            *DEFAULT_CONN_RESPONSES,
            ("systemctl restart --quiet", ("", False)),
        ]
    )
    state = State(current="b-digest", previous=["a-digest"])
    current = make_manifest("1.0.1", "dist-bbb")
    cmd, write_state, swap_current, _v, _iu = setup_rollback(
        monkeypatch, minimal_config, conn, state=state, current_manifest=current
    )
    assert exit_code_of(lambda: cmd()) == EXIT_ROLLBACK_FAILED
    swap_current.assert_called_once_with(conn, "/opt/fujin/testapp", "a-digest")
    write_state.assert_not_called()


def test_swap_current_failure_exits_6(monkeypatch, minimal_config):
    """A failure after the target is chosen is past the point of no return:
    a TransferError from swap_current is a failed rollback (6), never 1/4."""
    conn = FakeConn(DEFAULT_CONN_RESPONSES)
    state = State(current="b-digest", previous=["a-digest"])
    current = make_manifest("1.0.1", "dist-bbb")
    cmd, write_state, swap_current, _v, _iu = setup_rollback(
        monkeypatch, minimal_config, conn, state=state, current_manifest=current
    )
    swap_current.side_effect = fujin.transfer.TransferError("cannot swap")

    assert exit_code_of(lambda: cmd()) == EXIT_ROLLBACK_FAILED
    write_state.assert_not_called()


def test_install_units_failure_exits_6(monkeypatch, minimal_config):
    """install_units raises ServerOpsError: the rollback failed (6)."""
    conn = FakeConn(DEFAULT_CONN_RESPONSES)
    state = State(current="b-digest", previous=["a-digest"])
    current = make_manifest("1.0.1", "dist-bbb")
    cmd, write_state, swap_current, _v, install_units = setup_rollback(
        monkeypatch, minimal_config, conn, state=state, current_manifest=current
    )
    install_units.side_effect = fujin.serverops.ServerOpsError("daemon-reload failed")

    assert exit_code_of(lambda: cmd()) == EXIT_ROLLBACK_FAILED
    swap_current.assert_called_once_with(conn, "/opt/fujin/testapp", "a-digest")
    write_state.assert_not_called()


def test_write_state_failure_exits_6(monkeypatch, minimal_config):
    """The release is live but the state was not recorded: rollback failed."""
    conn = FakeConn(DEFAULT_CONN_RESPONSES)
    state = State(current="b-digest", previous=["a-digest"])
    current = make_manifest("1.0.1", "dist-bbb")
    cmd, write_state, _swap, _v, _iu = setup_rollback(
        monkeypatch, minimal_config, conn, state=state, current_manifest=current
    )
    write_state.side_effect = fujin.serverops.ServerOpsError("Failed to write state")

    assert exit_code_of(lambda: cmd()) == EXIT_ROLLBACK_FAILED


def test_no_restart_needed_when_nothing_changed(monkeypatch, minimal_config):
    # identical code entries + dist => restart_set covers neither env nor code
    conn = FakeConn(DEFAULT_CONN_RESPONSES)
    state = State(current="b-digest", previous=["a-digest"])
    current = make_manifest("1.0.1", "dist-aaa")
    cmd, *rest = setup_rollback(
        monkeypatch, minimal_config, conn, state=state, current_manifest=current
    )
    cmd()
    assert not any("systemctl restart" in c for c in conn.calls)


# ===========================================================================
# Convergence: template instances follow target replica counts
# ===========================================================================


def test_convergence_starts_missing_stops_extra(monkeypatch, minimal_config):
    minimal_config.replicas = {"worker": 2}
    conn = FakeConn(DEFAULT_CONN_RESPONSES)
    conn.responses.append(
        (
            "systemctl list-unit-files",
            (
                "testapp-worker@2.service enabled\n"
                "testapp-worker@3.service enabled\n"
                "testapp-worker@.service enabled\n"
                f"{fujin.serverops.PLAN_ENABLED_END}\n"
                f"{fujin.serverops.PLAN_LINKS_END}\n"
                "testapp-worker@2.service loaded active running\n",
                True,
            ),
        )
    )
    state = State(current="b-digest", previous=["a-digest"])
    current = make_manifest("1.0.1", "dist-bbb")
    cmd, *_ = setup_rollback(
        monkeypatch,
        minimal_config,
        conn,
        state=state,
        current_manifest=current,
        previous=True,
    )
    cmd()

    enable = fujin.serverops.enable_and_start.call_args.args[1]
    stop = fujin.serverops.stop_and_disable.call_args.args[1]
    # worker@1 is missing entirely; worker@2 is enabled and running.
    assert enable == ["testapp-worker@1.service"]
    assert stop == ["testapp-worker@3.service"]


# ===========================================================================
# Regression: generic *.d/ dropins + audit host
# ===========================================================================


def test_socket_and_timer_dropins_collected_as_dropins(monkeypatch, minimal_config):
    """Regression: only ``*.service.d/`` used to be special-cased, so a
    ``*.socket.d/``/``*.timer.d/`` dropin shipped raw into install_units as
    a unit file path. Match the deploy-side generic ``.d/`` handling."""
    files = default_files(
        extra={
            "systemd/testapp-web.socket": "[Socket]\nListenStream=/run/s\n",
            "systemd/testapp-web.socket.d/override.conf": "[Socket]\nSocketMode=0660\n",
            "systemd/testapp-backup.timer.d/override.conf": "[Timer]\nPersistent=true\n",
        }
    )
    conn = FakeConn(
        [
            *LAYOUT_RESPONSES,
            manifest_response("a-digest", "1.0.0", "dist-aaa", files=files),
            ("ls -1t releases/", ("b-digest\na-digest\n", True)),
            *[
                unit_response("a-digest", n, c)
                for n, c in sorted(
                    files.items(),
                    key=lambda kv: (".d/" in kv[0], kv[0]),
                    reverse=True,
                )
                if n.startswith("systemd/")
            ],
            ("ls -1t releases/", ("b-digest\na-digest\n", True)),
        ]
    )
    state = State(current="b-digest", previous=["a-digest"])
    current = make_manifest("1.0.1", "dist-bbb")
    cmd, _ws, _swap, _v, install_units = setup_rollback(
        monkeypatch,
        minimal_config,
        conn,
        state=state,
        current_manifest=current,
        to="a-digest",
    )
    cmd()

    call = install_units.call_args
    unit_files, dropins = call.args[2], call.args[3]
    assert "testapp-web.socket.d/override.conf" not in unit_files
    assert dropins["testapp-web.socket"] == {
        "override.conf": "[Socket]\nSocketMode=0660\n"
    }
    assert dropins["testapp-backup.timer"] == {
        "override.conf": "[Timer]\nPersistent=true\n"
    }
    assert set(unit_files) == {
        "testapp-web.service",
        "testapp-worker@.service",
        "testapp-web.socket",
    }


def test_rollback_audit_log_records_real_host(monkeypatch, minimal_config):
    """Regression: the rollback audit call passed the app name as ``host``."""
    conn = FakeConn(DEFAULT_CONN_RESPONSES)
    state = State(current="b-digest", previous=["a-digest"])
    current = make_manifest("1.0.1", "dist-bbb")
    log_calls = []
    monkeypatch.setattr(
        rollback,
        "log_operation",
        lambda *a, **k: log_calls.append((a, k)),
    )
    cmd, _ws, _swap, _v, _iu = setup_rollback(
        monkeypatch, minimal_config, conn, state=state, current_manifest=current
    )
    cmd()
    assert log_calls
    assert log_calls[0][1]["host"] == "example.com"
    assert log_calls[0][0][2] == "rollback"


def test_to_version_resolves_manifest_version(monkeypatch, minimal_config):
    """--to <version>: find the kept release whose manifest version matches."""
    conn = FakeConn(DEFAULT_CONN_RESPONSES)
    state = State(current="b-digest", previous=["a-digest"])
    current = make_manifest("1.0.1", "dist-bbb")
    cmd, _ws, swap_current, _v, _iu = setup_rollback(
        monkeypatch,
        minimal_config,
        conn,
        state=state,
        current_manifest=current,
        to="1.0.0",
    )
    cmd()
    swap_current.assert_called_once_with(conn, "/opt/fujin/testapp", "a-digest")


def test_to_unknown_version_hard_errors(monkeypatch, minimal_config):
    conn = FakeConn(DEFAULT_CONN_RESPONSES)
    state = State(current="b-digest", previous=["a-digest"])
    current = make_manifest("1.0.1", "dist-bbb")
    cmd, _ws, swap_current, _v, _iu = setup_rollback(
        monkeypatch,
        minimal_config,
        conn,
        state=state,
        current_manifest=current,
        to="9.9.9",
    )
    with pytest.raises(cappa.output.Exit) as excinfo:
        cmd()
    assert excinfo.value.code == 1
    swap_current.assert_not_called()


def test_to_version_uses_digest_dir_hint(monkeypatch, minimal_config):
    """A value that is neither digest-shaped nor a manifest version but does
    name an existing release dir resolves as that digest (release dirs are
    digest-named in practice)."""
    conn = FakeConn(DEFAULT_CONN_RESPONSES)
    state = State(current="b-digest", previous=["a-digest"])
    current = make_manifest("1.0.1", "dist-bbb")
    cmd, _ws, swap_current, _v, _iu = setup_rollback(
        monkeypatch,
        minimal_config,
        conn,
        state=state,
        current_manifest=current,
        to="a-digest",
    )
    cmd()
    swap_current.assert_called_once_with(conn, "/opt/fujin/testapp", "a-digest")


# ===========================================================================
# Version targeting / digest discrimination edge cases
# ===========================================================================


def test_to_version_matches_git_suffixed_manifest_version(monkeypatch, minimal_config):
    """In git checkouts the manifest records '<version>-<short-hash>'; a
    plain '--to <version>' still resolves to that release."""
    conn = FakeConn(
        [
            *LAYOUT_RESPONSES,
            ("ls -1t releases/", ("suff-digest\n", True)),
            manifest_response("suff-digest", "1.0.0-ab12cd3", "dist-000"),
            *unit_responses_for("suff-digest", default_files()),
        ]
    )
    state = State(current="suff-digest", previous=[], client_digest=None)
    current = make_manifest("1.0.0-ab12cd3", "dist-000")
    cmd, _ws, swap_current, _v, _iu = setup_rollback(
        monkeypatch,
        minimal_config,
        conn,
        state=state,
        current_manifest=current,
        to="1.0.0",
    )
    with pytest.raises(cappa.output.Exit) as excinfo:
        cmd()
    # target == current: clean no-op exit rather than a resolution error
    assert excinfo.value.code == 0


def test_to_version_ignores_gc_releases(monkeypatch, minimal_config):
    """Only kept releases are version candidates: a GC'd release carrying
    the version is no longer resolvable (never offered via raw ls output)."""
    conn = FakeConn(DEFAULT_CONN_RESPONSES)
    state = State(
        current="b-digest",
        previous=["a-digest"],
        deploys=[
            DeployRecord(manifest="a-digest", at="t0", result="ok"),
            DeployRecord(manifest="b-digest", at="t1", result="ok"),
            DeployRecord(manifest="c-digest", at="t2", result="ok"),
        ],
    )
    current = make_manifest("1.0.1", "dist-bbb")
    cmd, _ws, swap_current, _v, _iu = setup_rollback(
        monkeypatch,
        minimal_config,
        conn,
        state=state,
        current_manifest=current,
        to="1.0.2",
    )
    minimal_config.versions_to_keep = 2
    assert exit_code_of(lambda: cmd()) == EXIT_USER_ABORTED
    swap_current.assert_not_called()


def test_to_digest_incomplete_release_dir_hard_error(monkeypatch, minimal_config):
    """A release dir that exists without manifest.json is reported as an
    incomplete release, not a GC'd one."""
    e_digest = "e" * 64
    conn = FakeConn(
        [
            *LAYOUT_RESPONSES,
            ("ls -1t releases/", ("b-digest\n" + e_digest + "\n", True)),
            (f"test -d /opt/fujin/testapp/releases/{e_digest}", ("", True)),
        ]
    )
    state = State(current="b-digest", previous=["a-digest"])
    current = make_manifest("1.0.1", "dist-bbb")
    cmd, _ws, swap_current, _v, _iu = setup_rollback(
        monkeypatch,
        minimal_config,
        conn,
        state=state,
        current_manifest=current,
        to=e_digest,
    )
    with pytest.raises(cappa.output.Exit) as excinfo:
        cmd()
    assert excinfo.value.code == 1
    assert "incomplete" in (excinfo.value.message or "")
    swap_current.assert_not_called()


def test_to_digest_uppercase_normalizes_to_lowercase_path(monkeypatch, minimal_config):
    upper = "A" * 64
    conn = FakeConn(
        [
            *LAYOUT_RESPONSES,
            ("ls -1t releases/", ("b-digest\nlower" * 0 + f"{'a' * 64}\n", True)),
            manifest_response("a" * 64, "1.0.0", "dist-000"),
            (
                f"/opt/fujin/testapp/releases/{'a' * 64}/systemd/testapp-web.service",
                (WEB_UNIT, True),
            ),
            (
                f"/opt/fujin/testapp/releases/{'a' * 64}/systemd/testapp-worker@.service",
                (WORKER_TEMPLATE_UNIT, True),
            ),
            (
                f"/opt/fujin/testapp/releases/{'a' * 64}/systemd/common.d/hardening.conf",
                ("[Service]\nLimitNOFILE=65536", True),
            ),
        ]
    )
    state = State(current=("a" * 64).replace("a", "b") and "b-digest", previous=[])
    current = make_manifest("1.0.1", "dist-bbb")
    cmd, _ws, swap_current, _v, _iu = setup_rollback(
        monkeypatch,
        minimal_config,
        conn,
        state=state,
        current_manifest=current,
        to=upper,
    )
    cmd()
    swap_current.assert_called_once_with(conn, "/opt/fujin/testapp", "a" * 64)


def test_to_version_swaps_git_suffixed_release(monkeypatch, minimal_config):
    """The git-suffixed base-version compare drives a real swap when the
    matched release is not the current one."""
    conn = FakeConn(
        [
            *LAYOUT_RESPONSES,
            ("ls -1t releases/", ("suff-digest\nb-digest\n", True)),
            manifest_response("suff-digest", "1.0.0-ab12cd3", "dist-000"),
            *unit_responses_for("suff-digest", default_files()),
        ]
    )
    state = State(current="b-digest", previous=["suff-digest"])
    current = make_manifest("1.0.1", "dist-bbb")
    cmd, _ws, swap_current, _v, _iu = setup_rollback(
        monkeypatch,
        minimal_config,
        conn,
        state=state,
        current_manifest=current,
        to="1.0.0",
    )
    cmd()
    swap_current.assert_called_once_with(conn, "/opt/fujin/testapp", "suff-digest")
