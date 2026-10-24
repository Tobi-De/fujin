"""Tests for the deterministic release-tree manifest (fujin.manifest)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from fujin.manifest import (
    _fingerprint_data,
    bundle_key,
    build_entry,
    build_manifest,
    canonical_json,
    canonical_manifest_bytes,
    ChangeType,
    classify_path,
    config_fingerprint,
    decode_manifest,
    default_mode,
    DEFAULT_MODE_CODE,
    DEFAULT_MODE_ENV,
    diff_manifests,
    EntryChange,
    EntryKind,
    hash_bytes,
    InputFile,
    manifest_from_dict,
    manifest_to_dict,
    Manifest,
    release_digest,
    sha256_hex,
)
from fujin.config import Config, HostConfig, InstallationMode, SecretConfig


# =============================================================================================
# Helpers
# =============================================================================================


def make_manifest(
    files: dict[str, str | bytes | InputFile] | None = None,
    app_name: str = "testapp",
    version: str = "1.0.0",
    config_fingerprint_: str = "fp0",
    dist_sha256: str = "d" * 64,
    created_at=None,
) -> Manifest:
    return build_manifest(
        app_name=app_name,
        version=version,
        config_fingerprint=config_fingerprint_,
        dist_sha256=dist_sha256,
        files=files or {},
        created_at=created_at,
    )


def make_config(env_content: str = "") -> Config:
    return Config(
        app_name="testapp",
        version="1.0.0",
        build_command="echo building",
        installation_mode=InstallationMode.PY_PACKAGE,
        python_version="3.11",
        distfile="dist/testapp-{version}-py3-none-any.whl",
        hosts=[
            HostConfig(
                name="primary",
                address="example.com",
                user="deploy",
                env_content=env_content,
                password_env="DEPLOY_PASSWORD",
            ),
            HostConfig(name="secondary", address="other.example.com", user="deploy2"),
        ],
        replicas={"web": 2},
        secret_config=SecretConfig(adapter="1password", options={"vault": "ops"}),
    )


# =============================================================================================
# Entry-kind classification (assigned by source, never by config)
# =============================================================================================


class TestClassifyPath:
    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            (".env", EntryKind.ENV),
            ("Caddyfile", EntryKind.CADDY),
            ("systemd/web.service", EntryKind.UNIT),
            ("systemd/web.socket", EntryKind.UNIT),
            ("systemd/web.service.d/override.conf", EntryKind.UNIT),
            ("systemd/common.d/hardening.conf", EntryKind.UNIT),
            ("assets/static/logo.png", EntryKind.ASSET),
            ("assets/logo.png", EntryKind.ASSET),
            ("venv/lib/python3.11/site-packages/x.py", EntryKind.CODE),
            ("venv/bin/testapp", EntryKind.CODE),
            ("bin/testapp", EntryKind.CODE),
            ("bin/shim", EntryKind.CODE),
        ],
    )
    def test_classification_by_source(self, path, expected):
        assert classify_path(path) is expected

    def test_root_level_fallback_is_code(self):
        assert classify_path("stray-dist-file") is EntryKind.CODE

    def test_kind_is_ignored_if_specified_in_input(self):
        # A caller cannot override the kind via InputFile; it is derived from the path.
        entry = build_entry("assets/tool", InputFile(content=b"x", mode=0o755))
        assert entry.kind is EntryKind.ASSET


# =============================================================================================
# Entries: hashing, modes, symlinks
# =============================================================================================


class TestEntries:
    def test_content_hash(self):
        entry = build_entry("assets/logo.png", b"png-data")
        assert entry.sha256 == sha256_hex(b"png-data")
        assert entry.link is None

    def test_str_content_encoded_utf8(self):
        assert build_entry("Caddyfile", "example.com").sha256 == sha256_hex(
            b"example.com"
        )

    def test_default_modes(self):
        assert default_mode(EntryKind.CODE) == DEFAULT_MODE_CODE == 0o755
        assert default_mode(EntryKind.ENV) == DEFAULT_MODE_ENV == 0o640
        for kind in (EntryKind.ASSET, EntryKind.UNIT, EntryKind.CADDY):
            assert default_mode(kind) == 0o644

    def test_code_defaults_to_executable(self):
        entry = build_entry("bin/testapp", b"\x7fELF...")
        assert entry.mode == 0o755

    def test_env_defaults_to_group_readable(self):
        entry = build_entry(".env", "SECRET=1")
        assert entry.mode == 0o640

    def test_explicit_mode_overrides_default(self):
        entry = build_entry("assets/tool", InputFile(content=b"x", mode=0o755))
        assert entry.mode == 0o755

    def test_symlink_entry(self):
        entry = build_entry("venv/bin/python", InputFile(link="python3.11"))
        assert entry.link == "python3.11"
        assert entry.sha256 is None
        assert entry.kind is EntryKind.CODE

    def test_exactly_one_of_content_or_link(self):
        with pytest.raises(ValueError):
            build_entry("x", InputFile())
        with pytest.raises(ValueError):
            build_entry("x", InputFile(content=b"a", link="b"))

    def test_identical_content_identical_entry(self):
        assert build_entry("a", b"x") == build_entry("a", b"x")

    def test_entry_is_frozen(self):
        entry = build_entry("a", b"x")
        with pytest.raises(AttributeError):
            entry.path = "b"


# =============================================================================================
# Canonical bytes, determinism, digest
# =============================================================================================


class TestManifestCanonicalization:
    def test_build_manifest_assigns_kinds_and_hashes(self):
        manifest = make_manifest(
            files={
                "bin/testapp": b"binary",
                ".env": "APP_ENV=production",
                "Caddyfile": "example.com {",
                "systemd/web.service": "[Unit]",
                "assets/logo.png": b"png",
            }
        )
        assert manifest.entries["bin/testapp"].kind is EntryKind.CODE
        assert manifest.entries[".env"].kind is EntryKind.ENV
        assert manifest.entries["Caddyfile"].kind is EntryKind.CADDY
        assert manifest.entries["systemd/web.service"].kind is EntryKind.UNIT
        assert manifest.entries["assets/logo.png"].kind is EntryKind.ASSET

    def test_entries_sorted_in_canonical_bytes(self):
        manifest = make_manifest(files={"zebra": b"1", "alpha": b"2", ".env": "x"})
        bytes_ = canonical_manifest_bytes(manifest).decode()
        paths = [m["path"] for m in json.loads(bytes_)["entries"].values()]
        assert paths == sorted(paths)
        assert bytes_ == json.dumps(
            json.loads(bytes_), sort_keys=True, separators=(",", ":")
        )

    def test_canonical_bytes_compact_and_sorted(self):
        manifest = make_manifest(files={"a": b"x", "b": b"y"})
        bytes_ = canonical_manifest_bytes(manifest)
        assert b": " not in bytes_ and b", " not in bytes_
        obj = json.loads(bytes_)
        assert list(obj) == sorted(obj)

    def test_same_inputs_same_digest(self):
        files = {"bin/app": b"binary", ".env": "A=1", "Caddyfile": "example.com {}"}
        m1 = make_manifest(files=files, version="1.0.0")
        m2 = make_manifest(files=dict(files), version="1.0.0")
        assert release_digest(m1) == release_digest(m2)
        assert canonical_manifest_bytes(m1) == canonical_manifest_bytes(m2)

    def test_created_at_never_enters_digest(self):
        files = {"bin/app": b"binary", ".env": "A=1"}
        m_plain = make_manifest(files=files)
        m_stamped = make_manifest(
            files=files,
            created_at=datetime(2026, 10, 22, tzinfo=UTC),
        )
        assert m_stamped.created_at is not None
        assert release_digest(m_plain) == release_digest(m_stamped)
        assert canonical_manifest_bytes(m_plain) == canonical_manifest_bytes(m_stamped)
        # ...but it is part of the stored manifest.json representation
        assert "created_at" in json.loads(
            canonical_json(manifest_to_dict(m_stamped, include_created_at=True))
        )
        assert "created_at" not in canonical_manifest_bytes(m_stamped).decode()

    def test_different_content_different_digest(self):
        m1 = make_manifest(files={"bin/app": b"one"})
        m2 = make_manifest(files={"bin/app": b"two"})
        assert release_digest(m1) != release_digest(m2)

    def test_digest_is_digest_of_canonical_bytes(self):
        manifest = make_manifest(files={"bin/app": b"one"})
        assert release_digest(manifest) == sha256_hex(
            canonical_manifest_bytes(manifest)
        )

    def test_manifest_dict_round_trip(self):
        manifest = make_manifest(
            files={
                "bin/app": b"one",
                ".env": "A=1",
                "venv/bin/python": InputFile(link="python3.11"),
            }
        )
        as_dict = json.loads(
            canonical_json(manifest_to_dict(manifest, include_created_at=True))
        )
        rebuilt = manifest_from_dict(as_dict)
        assert rebuilt == manifest
        assert release_digest(rebuilt) == release_digest(manifest)

    def test_decode_manifest_round_trip(self):
        manifest = make_manifest(
            files={"bin/app": b"one", ".env": "A=1", "systemd/web.service": "[Unit]"}
        )
        encoded = canonical_json({**json.loads(canonical_manifest_bytes(manifest))})
        decoded = decode_manifest(encoded)
        assert decoded == manifest

    def test_decode_manifest_with_created_at_and_octal_modes(self):
        raw = json.dumps(
            {
                "schema": 1,
                "app_name": "testapp",
                "version": "1.0.0",
                "config_fingerprint": "fp",
                "dist_sha256": "d" * 64,
                "created_at": "2026-10-22T00:00:00+00:00",
                "entries": {
                    ".env": {
                        "path": ".env",
                        "sha256": "abc",
                        "mode": "0640",
                        "kind": "env",
                    },
                    "bin/app": {
                        "path": "bin/app",
                        "sha256": None,
                        "mode": "0755",
                        "kind": "code",
                        "link": "x",
                    },
                },
            }
        )
        decoded = decode_manifest(raw)
        assert decoded.created_at is not None
        assert decoded.entries[".env"].mode == 0o640
        assert decoded.entries["bin/app"].link == "x"
        assert decoded.entries["bin/app"].sha256 is None


# =============================================================================================
# Asset / env changes
# =============================================================================================


class TestChangeIsolation:
    def base_files(self):
        return {
            "bin/testapp": b"binary",
            ".env": "A=1",
            "Caddyfile": "example.com {}",
            "systemd/web.service": "[Unit]\nType=exec",
            "assets/logo.png": b"png",
        }

    def test_asset_change_flips_no_code_entry(self):
        before = make_manifest(files=self.base_files())
        after_files = dict(self.base_files())
        after_files["assets/logo.png"] = b"new-png"
        after = make_manifest(files=after_files)

        diff = diff_manifests(before, after)
        assert diff.by_kind(EntryKind.CODE) == []
        assert [c.path for c in diff.by_kind(EntryKind.ASSET)] == ["assets/logo.png"]
        assert release_digest(before) != release_digest(after)

    def test_env_change_new_digest_no_rebuilt_files(self):
        before = make_manifest(files=self.base_files())
        after_files = dict(self.base_files())
        after_files[".env"] = "A=2"
        after = make_manifest(files=after_files)

        assert release_digest(before) != release_digest(after)
        diff = diff_manifests(before, after)
        assert diff.by_kind(EntryKind.CODE) == []
        assert [c.path for c in diff.by_kind(EntryKind.ENV)] == [".env"]

    def test_unit_change_only_flips_that_unit(self):
        before = make_manifest(files=self.base_files())
        after_files = dict(self.base_files())
        after_files["systemd/web.service"] = "[Unit]\nType=exec\nRestart=always"
        after = make_manifest(files=after_files)

        diff = diff_manifests(before, after)
        changed_paths = [c.path for c in diff.by_kind(EntryKind.UNIT)]
        assert changed_paths == ["systemd/web.service"]
        assert diff.by_kind(EntryKind.CODE) == []


# =============================================================================================
# Diff
# =============================================================================================


class TestDiffManifests:
    def prev(self):
        return make_manifest(
            files={
                "bin/app": b"code",
                "assets/a.png": b"a",
                "assets/gone.png": b"g",
                "systemd/web.service": "[Unit]",
                "systemd/worker@.service": "[Unit]",
                "Caddyfile": "example.com {}",
                ".env": "A=1",
            }
        )

    def new(self):
        return make_manifest(
            files={
                "bin/app": b"code2",
                "assets/a.png": b"a",
                "assets/new.png": b"n",
                "systemd/web.service": "[Unit]\nRestart=always",
                "systemd/worker@.service": "[Unit]",
                "Caddyfile": "example.com {}",
                ".env": "A=2",
            }
        )

    def test_added_removed_modified(self):
        diff = diff_manifests(self.prev(), self.new())
        by_change = {c.path: c.change for c in diff.changes}
        assert by_change == {
            "assets/gone.png": ChangeType.REMOVED,
            "assets/new.png": ChangeType.ADDED,
            "bin/app": ChangeType.MODIFIED,
            ".env": ChangeType.MODIFIED,
            "systemd/web.service": ChangeType.MODIFIED,
        }
        assert diff.changes == sorted(diff.changes, key=lambda c: c.path)

    def test_grouped_contains_all_kinds(self):
        diff = diff_manifests(self.prev(), self.new())
        grouped = diff.grouped()
        assert set(grouped) == set(EntryKind)
        assert {c.change for c in grouped[EntryKind.ASSET]} == {
            ChangeType.REMOVED,
            ChangeType.ADDED,
        }
        assert {c.path for c in grouped[EntryKind.ASSET]} == {
            "assets/gone.png",
            "assets/new.png",
        }
        assert grouped[EntryKind.CADDY] == []
        assert [c.path for c in grouped[EntryKind.UNIT]] == ["systemd/web.service"]
        assert [c.path for c in grouped[EntryKind.ENV]] == [".env"]
        assert [c.path for c in grouped[EntryKind.CODE]] == ["bin/app"]

    def test_kinds_changed_and_is_empty(self):
        diff = diff_manifests(self.prev(), self.new())
        assert diff.kinds_changed == {
            EntryKind.UNIT,
            EntryKind.ENV,
            EntryKind.CODE,
            EntryKind.ASSET,
        }
        same = diff_manifests(self.prev(), self.prev())
        assert same.is_empty
        assert same.kinds_changed == set()

    def test_removed_keeps_old_kind(self):
        m1 = make_manifest(files={"assets/x.png": b"a", "bin/app": b"b"})
        m2 = make_manifest(files={"bin/app": b"b"})
        diff = diff_manifests(m1, m2)
        change = diff.by_kind(EntryKind.ASSET)[0]
        assert change.change is ChangeType.REMOVED
        assert change.old.sha256 == sha256_hex(b"a")
        assert change.new is None

    def test_kind_property(self):
        change = EntryChange(path="bin/app", change=ChangeType.MODIFIED)
        with pytest.raises(AssertionError):  # no entry data at all
            _ = change.kind

    def test_identical_manifests_empty_diff(self):
        files = {
            "bin/app": b"code",
            ".env": "A=1",
            "systemd/web.service": "[Unit]",
            "Caddyfile": "example.com {}",
        }
        assert diff_manifests(
            make_manifest(files=files), make_manifest(files=files)
        ).is_empty


# =============================================================================================
# Config fingerprint: secret exclusion + determinism
# =============================================================================================


class TestConfigFingerprint:
    def test_deterministic(self):
        assert config_fingerprint(make_config()) == config_fingerprint(make_config())

    def test_excludes_secret_values(self):
        config = make_config(
            env_content="SECRET_TOKEN=super-secret-value\nPLAIN=public"
        )
        fingerprint = config_fingerprint(config)
        assert "super-secret-value" not in fingerprint
        assert "public" not in fingerprint  # values are not fingerprinted either

    def test_env_key_set_changes_fingerprint(self):
        fp1 = config_fingerprint(make_config(env_content="A=1\n"))
        fp2 = config_fingerprint(make_config(env_content="A=1\nB=2\n"))
        assert fp1 != fp2

    def test_replicas_excluded(self):
        config = make_config()
        fp1 = config_fingerprint(config)
        config.replicas["web"] = 99
        assert config_fingerprint(config) == fp1

    def test_version_excluded(self):
        config = make_config()
        fp1 = config_fingerprint(config)
        config.version = "9.9.9"
        assert config_fingerprint(config) == fp1

    def test_hosts_env_content_value_change_no_fingerprint_change(self):
        config = make_config(env_content="A=1")
        fp1 = config_fingerprint(config)
        config.hosts[0].env_content = "A=different-value"
        assert config_fingerprint(config) == fp1

    def test_config_change_flips_fingerprint(self):
        config = make_config()
        fp1 = config_fingerprint(config)
        config.build_command = "echo different"
        assert config_fingerprint(config) != fp1

    def test_host_env_key_change_flips_fingerprint(self):
        config = make_config(env_content="A=1")
        fp1 = config_fingerprint(config)
        config.hosts[0].env_content = "A=1\nNEW_KEY=2"
        assert config_fingerprint(config) != fp1

    def test_fingerprint_is_sha256_of_canonical_json(self):
        config = make_config()
        fp = config_fingerprint(config)
        assert fp == sha256_hex(canonical_json(_fingerprint_data(config)))


# =============================================================================================
# Bundle key (#81)
# =============================================================================================


class TestBundleKey:
    def make(
        self,
        config=None,
        units=None,
        assets=None,
        caddyfile=None,
        build_env=None,
        requirements=None,
    ):
        return bundle_key(
            config or make_config(),
            unit_sources=units or {"web.service": "[Unit]"},
            assets=assets,
            caddyfile=caddyfile,
            build_env=build_env,
            requirements=requirements,
        )

    def test_stable_for_same_inputs(self):
        k1 = self.make(
            units={"web.service": b"[Unit]"},
            assets={"a.png": b"a"},
            caddyfile="example.com {}",
        )
        k2 = self.make(
            units={"web.service": b"[Unit]"},
            assets={"a.png": b"a"},
            caddyfile="example.com {}",
        )
        assert k1 == k2

    def test_unit_change_busts_key(self):
        k1 = self.make(units={"web.service": b"[Unit]"})
        k2 = self.make(units={"web.service": b"[Unit]\nRestart=always"})
        assert k1 != k2

    def test_asset_and_caddy_changes_bust_key(self):
        k1 = self.make(assets={"a.png": b"a"})
        k2 = self.make(assets={"a.png": b"b"})
        assert k1 != k2
        k3 = self.make(caddyfile="example.com {}")
        k4 = self.make(caddyfile="other.com {}")
        assert k3 != k4

    def test_requirements_change_busts_key(self):
        k1 = self.make(requirements="django==5.0")
        k2 = self.make(requirements="django==5.1")
        assert k1 != k2

    def test_build_env_change_busts_key(self):
        k1 = self.make(build_env={"NODE_ENV": "production"})
        k2 = self.make(build_env={"NODE_ENV": "development"})
        assert k1 != k2

    def test_build_command_change_busts_key(self):
        k1 = self.make()
        k2 = self.make(make_config())
        config = make_config()
        config.build_command = "echo different"
        k3 = self.make(config)
        assert k1 == k2 != k3

    def test_excludes_secret_env_values(self):
        # The rendered .env / secret values must never influence the bundle key
        k1 = self.make(make_config(env_content="SECRET_TOKEN=original"))
        k2 = self.make(make_config(env_content="SECRET_TOKEN=rotated-secret"))
        assert k1 == k2

    def test_version_busts_key_not_replicas(self):
        """The version must bust the key: the built dist embeds it (wheel
        names/metadata), so a version bump cannot silently deploy a stale
        dist file. ``replicas`` stays out (#79 handles scaling separately)."""
        config = make_config()
        k_current = self.make(config)
        config.replicas = {"web": 10}
        assert self.make(config) == k_current
        config = make_config()
        k1 = self.make(config)
        config.version = "9.9.9"
        assert self.make(config) != k1

    def test_key_is_hex_sha256(self):
        assert len(self.make()) == 64
        int(self.make(), 16)


def test_sha256_hex_and_hash_bytes():
    assert (
        sha256_hex(b"")
        == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )
    assert hash_bytes("héllo") == hash_bytes("héllo".encode())
