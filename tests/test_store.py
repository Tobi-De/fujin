from __future__ import annotations

import json
from dataclasses import dataclass, field

import msgspec
import pytest

from fujin import store


@dataclass(frozen=True)
class FakeEntry:
    kind: str
    sha256: str = ""
    link: str | None = None
    mode: str = "0644"


@dataclass(frozen=True)
class FakeManifest:
    entries: dict[str, FakeEntry] = field(default_factory=dict)
    dist_sha256: str = "dist-1"


def manifest(entries: dict[str, FakeEntry], dist: str = "dist-1") -> FakeManifest:
    return FakeManifest(entries=entries, dist_sha256=dist)


def full_manifest(dist: str = "dist-1") -> FakeManifest:
    return manifest(
        {
            "systemd/app-web.service": FakeEntry("unit", "u1"),
            "systemd/app-web.socket": FakeEntry("unit", "u2"),
            "systemd/app-timer.timer": FakeEntry("unit", "u3"),
            "Caddyfile": FakeEntry("caddy", "c1"),
            ".env": FakeEntry("env", "e1"),
            "public/logo.svg": FakeEntry("asset", "a1"),
            "app/main.py": FakeEntry("code", "code1"),
        },
        dist=dist,
    )


ENV_FED = ["app-web.service"]
APP_UNITS = ["app-web", "app-worker"]


class TestStateRoundTrip:
    def test_canonical_bytes_and_parse(self):
        state = store.State(current="abc", previous=["def"], units_installed_from="abc")
        data = store.serialize_state(state)
        reparsed = store.parse_state(data)
        assert reparsed == state

    def test_parse_tolerates_sha256_prefixed_digests(self):
        raw = json.dumps(
            {
                "current": "sha256:abc",
                "previous": ["sha256:def"],
                "units_installed_from": "sha256:abc",
                "deploys": [
                    {
                        "manifest": "sha256:abc",
                        "at": "2026-01-01T00:00:00Z",
                        "result": "ok",
                    }
                ],
            }
        ).encode()
        state = store.parse_state(raw)
        assert state.current == "abc"
        assert state.previous == ["def"]
        assert state.deploys[0].manifest == "abc"

    def test_compact_single_line_output(self):
        data = store.serialize_state(store.State(current="abc"))
        assert b"\n" not in data

    def test_record_deploy_purges_previous_and_logs(self):
        state = store.parse_state(b"{}")
        state = store.record_deploy(state, "r2", at="t2")
        state = store.record_deploy(state, "r3", at="t3")
        assert state.current == "r3"
        assert state.previous == ["r2"]
        assert [(d.manifest, d.result) for d in state.deploys] == [
            ("r2", "ok"),
            ("r3", "ok"),
        ]

    def test_record_deploy_to_same_digest_no_previous(self):
        state = store.parse_state(b"{}")
        state = store.record_deploy(state, "r1", at="t1")
        state = store.record_deploy(state, "r1", at="t2")
        assert state.previous == []

    def test_record_rollback_single_level(self):
        state = store.parse_state(b"{}")
        state = store.record_deploy(state, "r1", at="t1")
        state = store.record_deploy(state, "r2", at="t2")
        rolled = store.record_rollback(state, "r1", at="t3")
        assert rolled.current == "r1"
        assert rolled.previous == ["r2"]
        assert rolled.deploys[-1].result == "rollback"

    def test_write_state_atomic_rename(self, tmp_path):
        target = tmp_path / "state.json"
        store.write_state(target, store.State(current="abc"))
        assert json.loads(target.read_text()) == {
            "current": "abc",
            "previous": [],
            "units_installed_from": None,
            "deploys": [],
            "client_digest": None,
        }
        assert not (tmp_path / "state.json.tmp").exists()

    def test_write_state_stream_target(self):
        chunks: list[bytes] = []

        class Sink:
            def write(self, data: bytes) -> int:
                chunks.append(data)
                return len(data)

        store.write_state(Sink(), store.State(current="abc"))
        assert b"abc" in chunks[0]


class TestActivationSteps:
    def test_pre_swap_steps_abort(self):
        names = [s.name for s in store.activation_plan()]
        assert names == [
            "ensure",
            "prepare",
            "transfer",
            "install",
            "verify",
            "commit",
            "units",
            "caddy",
            "swap",
            "restart",
            "ready",
            "confirm",
        ]
        swap_index = names.index("swap")
        for step in store.ACTIVATION_STEPS[: swap_index + 1]:
            assert step.on_failure == "abort", step

    def test_post_swap_steps_rollback(self):
        post = [s for s in store.ACTIVATION_STEPS if s.on_failure == "rollback"]
        assert [s.name for s in post] == ["restart", "ready", "confirm"]

    def test_noop_plan_skips_transfer_steps(self):
        names = [
            s.name
            for s in store.activation_plan(
                skip=("transfer", "install", "verify", "commit")
            )
        ]
        assert "transfer" not in names and "restart" in names

    def test_rollback_steps_never_recurse(self):
        assert all(s.on_failure == "abort" for s in store.ROLLBACK_STEPS)


class TestRestartSet:
    def test_unchanged_release_no_restart(self):
        prev = full_manifest()
        new = full_manifest()
        assert store.restart_set(prev, new, APP_UNITS) == frozenset()

    def test_unit_change_restarts_exactly_that_unit(self):
        prev = full_manifest()
        new = manifest(
            {e[0]: e[1] for e in prev.entries.items()}
            | {"systemd/app-worker.service": FakeEntry("unit", "u9")}
        )
        got = store.restart_set(prev, new, ENV_FED + ["app-worker.service"])
        assert got == frozenset({"app-worker.service"})

    def test_env_change_restarts_env_fed_units_only(self):
        prev = full_manifest()
        new = manifest({**prev.entries, ".env": FakeEntry("env", "changed")})
        got = store.restart_set(prev, new, ["app-web.service"])
        assert got == frozenset({"app-web.service"})

    def test_code_change_restarts_all_app_units(self):
        prev = full_manifest()
        new = manifest({**prev.entries, "app/main.py": FakeEntry("code", "code2")})
        got = store.restart_set(prev, new, ["app-web.service", "app-worker.service"])
        assert got == frozenset({"app-web.service", "app-worker.service"})

    def test_dist_sha_change_counts_as_code(self):
        prev = full_manifest("dist-1")
        new = manifest(prev.entries, dist="dist-2")
        got = store.restart_set(prev, new, ["app-web.service"])
        assert got == frozenset({"app-web.service"})

    def test_asset_change_restarts_nothing(self):
        prev = full_manifest()
        new = manifest({**prev.entries, "public/logo.svg": FakeEntry("asset", "a2")})
        got = store.restart_set(prev, new, ["app-web.service"])
        assert got == frozenset()

    def test_caddy_change_never_contributes(self):
        prev = full_manifest()
        new = manifest({**prev.entries, "Caddyfile": FakeEntry("caddy", "c2")})
        got = store.restart_set(prev, new, ["app-web.service"])
        assert got == frozenset()

    def test_oneshot_units_excluded(self):
        prev = full_manifest()
        new = manifest({**prev.entries, "app/main.py": FakeEntry("code", "code2")})
        got = store.restart_set(
            prev, new, ["app-web", "db-migrate"], oneshot_units=["db-migrate"]
        )
        assert "db-migrate" not in got
        assert got == frozenset({"app-web"})

    def test_first_deploy_everything_considered(self):
        got = store.restart_set(None, full_manifest(), ["app-web.service"])
        # first deploy: all unit entries + env + code restart
        assert got == frozenset(
            {"app-web.service", "app-web.socket", "app-timer.timer"}
        )

    def test_removed_unit_entry_excluded_from_restart(self):
        prev = full_manifest()
        entries = {
            k: v for k, v in prev.entries.items() if k != "systemd/app-web.socket"
        }
        got = store.restart_set(
            prev, manifest(entries), ["app-web.socket", "app-web.service"]
        )
        # removal is the convergence/stop layer's job - never a restart
        assert got == frozenset()

    def test_common_dropin_change_restarts_env_fed_units(self):
        prev = full_manifest()
        new = manifest(
            {**prev.entries, "systemd/common.d/base.conf": FakeEntry("unit", "d2")}
        )
        got = store.restart_set(prev, new, ["app-web.service", "app-worker.service"])
        assert got == frozenset({"app-web.service", "app-worker.service"})

    def test_dropin_change_maps_to_parent_unit(self):
        prev = full_manifest()
        new = manifest(
            {
                **prev.entries,
                "systemd/app-worker.service.d/limit.conf": FakeEntry("unit", "d2"),
            }
        )
        got = store.restart_set(prev, new, ["app-worker.service"])
        assert got == frozenset({"app-worker.service"})

    def test_unit_entry_name_mapping(self):
        assert store.unit_entry_name("systemd/a@.service") == "a@.service"
        assert store.unit_entry_name("systemd/a.service.d/x.conf") == "a.service"
        assert store.unit_entry_name("Caddyfile") is None
        assert store.unit_entry_name("systemd/common.d/base.conf") is None
        assert store.unit_entry_name("systemd/common.d/nested/x.conf") is None
        assert store.unit_entry_name("systemd/nested/a.service") is None


class TestConvergence:
    def test_scale_up(self):
        start, stop = store.convergence({"w@1", "w@2", "w@3"}, {"w@1"}, {"w@1"})
        assert start == {"w@2", "w@3"}
        assert stop == frozenset()

    def test_scale_down(self):
        start, stop = store.convergence(
            {"w@1"}, {"w@1", "w@2", "w@3"}, {"w@1", "w@2", "w@3"}
        )
        assert start == frozenset()
        assert stop == {"w@2", "w@3"}

    def test_stopped_but_enabled_is_started(self):
        start, stop = store.convergence({"w@1"}, {"w@1"}, set())
        assert start == {"w@1"}
        assert stop == frozenset()

    def test_active_but_disabled_is_started(self):
        start, stop = store.convergence({"w@1", "w@2"}, {"w@1"}, {"w@1", "w@2"})
        assert start == {"w@2"}
        assert stop == frozenset()

    def test_normalization_drops_service_suffix(self):
        start, stop = store.convergence({"w@1.service"}, {"w@1"}, {"w@1.service"})
        assert (start, stop) == (frozenset(), frozenset())

    def test_bare_template_never_desired(self):
        with pytest.raises(ValueError):
            store.convergence(["worker@"], [], [])

    def test_desired_instances_never_bare_template(self):
        want = store.desired_instances("fujin-worker", 3)
        assert want == {"fujin-worker@1", "fujin-worker@2", "fujin-worker@3"}
        assert "fujin-worker@" not in want
        assert "fujin-worker@.service" not in want

    def test_desired_instances_rejects_zero(self):
        with pytest.raises(ValueError):
            store.desired_instances("w", 0)

    def test_empty_desired_stops_everything(self):
        start, stop = store.convergence([], ["w@1", "w@2"], ["w@2"])
        assert start == frozenset()
        assert stop == {"w@1", "w@2"}

    def test_instance_sort_key_parses_numeric_index(self):
        assert store.instance_sort_key("worker@2") == 2
        assert store.instance_sort_key("worker@10.service") == 10

    def test_instance_sort_key_orders_numerically_not_lexically(self):
        units = ["worker@2.service", "worker@10.service", "worker@1.service"]
        assert sorted(units, key=lambda u: (-store.instance_sort_key(u), u)) == [
            "worker@10.service",
            "worker@2.service",
            "worker@1.service",
        ]

    def test_instance_sort_key_non_numeric_is_zero(self):
        assert store.instance_sort_key("x@canary") == 0
        assert store.instance_sort_key("x@canary.service") == 0
        assert store.instance_sort_key("worker@") == 0
        assert store.instance_sort_key("worker.service") == 0


class TestGCPlan:
    def test_kill_list_excludes_keep_set(self):
        releases = ["r5", "r4", "r3", "r2", "r1"]  # newest-first
        kills = store.gc_plan(
            current="r5", previous=["r4"], releases=releases, versions_to_keep=3
        )
        assert kills == ("r1", "r2")

    def test_previous_always_kept_even_beyond_keep(self):
        releases = ["r3", "r2", "r1"]
        kills = store.gc_plan(
            current="r3", previous=["r1"], releases=releases, versions_to_keep=1
        )
        assert kills == ("r2",)

    def test_current_always_kept(self):
        releases = ["r3", "r2", "r1"]
        kills = store.gc_plan(
            current="r1", previous=[], releases=releases, versions_to_keep=1
        )
        assert kills == ("r2", "r3")

    def test_versions_to_keep_zero_keeps_previous_and_current(self):
        releases = ["r4", "r3", "r2", "r1"]
        kills = store.gc_plan(
            current="r4", previous=["r3"], releases=releases, versions_to_keep=0
        )
        assert kills == ("r1", "r2")

    def test_more_keep_than_releases_no_kills(self):
        releases = ["r2", "r1"]
        assert store.gc_plan("r2", ["r1"], releases, versions_to_keep=5) == ()


class TestNoop:
    def test_same_digest_is_noop(self):
        state = store.parse_state(b"{}")
        state = store.record_deploy(state, "r1", at="t")
        assert store.is_noop("r1", state) is True

    def test_differs_not_noop(self):
        state = store.parse_state(b"{}")
        state = store.record_deploy(state, "r1", at="t")
        assert store.is_noop("r2", state) is False

    def test_first_deploy_not_noop(self):
        assert store.is_noop("r1", store.parse_state(b"{}")) is False


class TestStateJsonSchema:
    def test_matches_contract_shape(self):
        state = store.parse_state(
            json.dumps(
                {
                    "current": "sha256:abc",
                    "previous": ["sha256:def"],
                    "units_installed_from": "sha256:abc",
                    "deploys": [
                        {"manifest": "sha256:abc", "at": "RFC", "result": "ok"}
                    ],
                    "client_digest": "sha256:xyz",
                }
            )
        )
        assert msgspec.to_builtins(state) == {
            "current": "abc",
            "previous": ["def"],
            "units_installed_from": "abc",
            "deploys": [{"manifest": "abc", "at": "RFC", "result": "ok"}],
            "client_digest": "xyz",
        }

    def test_older_state_without_client_digest(self):
        raw = json.dumps({"current": "abc", "previous": [], "deploys": []}).encode()
        state = store.parse_state(raw)
        assert state.client_digest is None
        # no-op check falls back to current for older state files
        assert store.is_noop("abc", state) is True

    def test_record_deploy_records_both_digests(self):
        state = store.record_deploy(store.parse_state(b"{}"), "final", "t", "client")
        assert state.current == "final"
        assert state.client_digest == "client"
        assert store.is_noop("client", state) is True
        assert store.is_noop("final", state) is False

    def test_client_digest_defaults_to_final_digest(self):
        state = store.record_deploy(store.parse_state(b"{}"), "r1", "t")
        assert state.client_digest == "r1"


class TestGCNone:
    def test_versions_to_keep_none_keeps_everything(self):
        releases = ["r5", "r4", "r3", "r2", "r1"]
        assert store.gc_plan("r5", ["r4"], releases, None) == ()
        assert store.gc_plan("r5", ["r4"], releases) == ()
