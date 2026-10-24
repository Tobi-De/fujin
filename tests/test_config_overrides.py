"""Tests for per-host config overrides (four-layer merge)."""

from __future__ import annotations

import pytest

from fujin.config import Config, SecretConfig, merge_host_layers
from fujin.errors import ImproperlyConfiguredError

BASE_FUJIN_TOML = """
app = "testapp"
version = "1.0.0"
build_command = "echo building"
installation_mode = "python-package"
python_version = "3.11"
distfile = "dist/testapp-{version}-py3-none-any.whl"
replicas = { web = 1 }

[[hosts]]
name = "staging"
address = "staging.example.com"
user = "deploy"
port = 2200

[[hosts]]
name = "production"
address = "example.com"
user = "deploy"
port = 22
"""

SINGLE_HOST_FUJIN_TOML = """
app = "testapp"
version = "1.0.0"
build_command = "echo building"
installation_mode = "python-package"
python_version = "3.11"
distfile = "dist/testapp-{version}-py3-none-any.whl"

[[hosts]]
address = "example.com"
user = "deploy"
"""


def write_fujin_toml(tmp_path, content: str):
    (tmp_path / "fujin.toml").write_text(content)


def write_override(tmp_path, name: str, content: str):
    hosts_dir = tmp_path / ".fujin" / "hosts"
    hosts_dir.mkdir(parents=True, exist_ok=True)
    (hosts_dir / f"{name}.toml").write_text(content)


# ============================================================================
# _deep_merge semantics
# ============================================================================


def test_deep_merge_scalars_replace():
    base = {"a": 1, "b": {"c": 2}}
    assert merge(base, {"a": 9, "d": 3}) == {
        "a": 9,
        "b": {"c": 2},
        "d": 3,
    }


def merge(base, override):  # thin alias for readability below
    from fujin.config import _deep_merge

    return _deep_merge(base, override)


def test_deep_merge_maps_deep_merge_recursively():
    base = {"a": {"b": 1, "c": 2}}
    override = {"a": {"c": 3, "d": 4}}
    assert merge(base, override) == {"a": {"b": 1, "c": 3, "d": 4}}


def test_deep_merge_arrays_replace():
    base = {"a": [1, 2, 3], "b": {"hooks": ["x", "y"]}}
    override = {"a": [7], "b": {"hooks": ["z"]}}
    assert merge(base, override) == {"a": [7], "b": {"hooks": ["z"]}}


def test_deep_merge_idempotence():
    merged = merge({"a": 1, "b": {"c": [1, 2]}}, {"b": {"c": [3]}})
    assert merge(merged, {"b": {"c": [3]}}) == merged
    assert merge(merged, merged) == merged


def test_deep_merge_associativity():
    a = {"x": 1, "y": {"a": 1, "b": 2}, "z": [1]}
    b = {"y": {"b": 9}, "z": [2]}
    c = {"y": {"c": 5}, "x": 7}
    assert merge(a, merge(b, c)) == merge(merge(a, b), c)


def test_deep_merge_does_not_mutate_inputs():
    base = {"a": {"b": 1}}
    override = {"a": {"c": 2}}
    merge(base, override)
    assert base == {"a": {"b": 1}}
    assert override == {"a": {"c": 2}}


# ============================================================================
# Layer 4: .fujin/hosts/<name>.toml
# ============================================================================


def test_host_override_file_overrides_host_fields(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_fujin_toml(tmp_path, BASE_FUJIN_TOML)
    write_override(
        tmp_path,
        "staging",
        "port = 4321\nnative_ssh_auth = true\n",
    )

    config = Config.read()
    staging = config.select_host("staging")
    production = config.select_host("production")

    assert staging.port == 4321
    assert staging.native_ssh_auth is True
    assert staging.address == "staging.example.com"  # base values kept
    assert production.port == 22
    assert production.native_ssh_auth is False


def test_host_override_isolated_per_host(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_fujin_toml(tmp_path, BASE_FUJIN_TOML)
    write_override(tmp_path, "staging", "port = 4321\n")

    staging = Config.read("staging").select_host("staging")
    production = Config.read("production").select_host("production")

    assert staging.port == 4321
    assert production.port == 22


def test_host_override_file_app_level_keys(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_fujin_toml(tmp_path, BASE_FUJIN_TOML)
    write_override(tmp_path, "staging", "versions_to_keep = 10\n")

    config = Config.read("staging")
    prod_config = Config.read("production")

    assert config.versions_to_keep == 10
    assert prod_config.versions_to_keep == 5  # schema default untouched


def test_host_override_non_selected_host_ignores_app_level(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_fujin_toml(tmp_path, BASE_FUJIN_TOML)
    write_override(tmp_path, "staging", "versions_to_keep = 10\n")

    config = Config.read("production")
    assert config.versions_to_keep == 5


def test_no_override_files_unchanged_behavior(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_fujin_toml(tmp_path, BASE_FUJIN_TOML)

    config = Config.read()
    assert config.select_host("staging").port == 2200
    assert config.select_host("production").port == 22
    assert config.app_name == "testapp"


def test_single_unnamed_host_without_overrides(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_fujin_toml(tmp_path, SINGLE_HOST_FUJIN_TOML)

    config = Config.read()
    assert config.select_host(None).address == "example.com"


def test_replicas_dict_deep_merges_from_override(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_fujin_toml(tmp_path, BASE_FUJIN_TOML)
    write_override(tmp_path, "staging", "replicas = { worker = 2 }\n")

    config = Config.read("staging")
    assert config.replicas == {"web": 1, "worker": 2}


def test_unknown_host_override_file_hard_errors(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_fujin_toml(tmp_path, BASE_FUJIN_TOML)
    write_override(tmp_path, "stagging", "port = 1\n")  # typo

    with pytest.raises(ImproperlyConfiguredError) as exc_info:
        Config.read()
    assert "stagging" in exc_info.value.message.lower()


def test_unknown_host_flag_hard_errors(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_fujin_toml(tmp_path, BASE_FUJIN_TOML)

    with pytest.raises(ImproperlyConfiguredError) as exc_info:
        Config.read("nope")
    assert "nope" in exc_info.value.message.lower()


def test_override_file_spillover_host_errors(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_fujin_toml(tmp_path, BASE_FUJIN_TOML)
    write_override(tmp_path, "staging", "[hosts]\nproduction = { port = 1 }\n")

    with pytest.raises(ImproperlyConfiguredError) as exc_info:
        Config.read("staging")
    assert "production" in exc_info.value.message.lower()


# ============================================================================
# Layer 3: [hosts.<name>] map form
# ============================================================================


def test_hosts_map_form(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_fujin_toml(
        tmp_path,
        """app = "testapp"
version = "1.0.0"
build_command = "echo building"
installation_mode = "python-package"
python_version = "3.11"
distfile = "dist/testapp-{version}-py3-none-any.whl"

[hosts.staging]
address = "staging.example.com"
user = "deploy"
port = 3000
""",
    )

    config = Config.read()
    assert config.select_host("staging").port == 3000


def test_hosts_map_form_with_override_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_fujin_toml(
        tmp_path,
        """app = "testapp"
version = "1.0.0"
build_command = "echo building"
installation_mode = "python-package"
python_version = "3.11"
distfile = "dist/testapp-{version}-py3-none-any.whl"

[hosts.staging]
address = "staging.example.com"
user = "deploy"
port = 3000
""",
    )
    write_override(tmp_path, "staging", "port = 3001\n")

    config = Config.read("staging")
    assert config.select_host("staging").port == 3001


def test_hosts_map_form_unknown_override_file_errors(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_fujin_toml(
        tmp_path,
        """app = "t"
version = "1.0.0"
build_command = "e"
installation_mode = "python-package"
python_version = "3.11"
distfile = "d"

[hosts.staging]
address = "s"
user = "d"
""",
    )
    write_override(tmp_path, "production", "port = 1\n")

    with pytest.raises(ImproperlyConfiguredError) as exc_info:
        Config.read()
    assert "production" in exc_info.value.message.lower()


# ============================================================================
# Strictness: unknown keys
# ============================================================================


def test_unknown_top_level_key_hard_errors(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_fujin_toml(tmp_path, SINGLE_HOST_FUJIN_TOML + '\nscretz = "typo"\n')

    with pytest.raises(ImproperlyConfiguredError) as exc_info:
        Config.read()
    assert "scretz" in exc_info.value.message.lower()


def test_unknown_host_field_hard_errors(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_fujin_toml(tmp_path, SINGLE_HOST_FUJIN_TOML + "\nports = 22\n")

    with pytest.raises(ImproperlyConfiguredError) as exc_info:
        Config.read()
    assert "ports" in exc_info.value.message.lower()


# ============================================================================
# merge_host_layers raised directly (layered precedence)
# ============================================================================


BASE_RAW = {
    "app": "testapp",
    "version": "1.0.0",
    "build_command": "echo building",
    "installation_mode": "python-package",
    "python_version": "3.11",
    "distfile": "dist/testapp-{version}-py3-none-any.whl",
    "hosts": [
        {"name": "staging", "address": "s.example.com", "user": "deploy"},
        {"name": "production", "address": "p.example.com", "user": "deploy"},
    ],
}


def test_layers_merge_over_host_fields():
    config = merge_host_layers(
        BASE_RAW,
        {"staging": {"port": 4321}},
        host_name="production",  # staging file not applied for production
    )
    assert config.select_host("staging").port == 22
    assert config.select_host("production").port == 22


def test_per_host_secrets_override(tmp_path, monkeypatch):
    """Per-host secrets (#75): host-level [secrets] overrides the global,
    hosts without one keep the global adapter."""
    write_fujin_toml(tmp_path, BASE_FUJIN_TOML + '\n[secrets]\nadapter = "bitwarden"\n')
    write_override(tmp_path, "staging", '[secrets]\nadapter = "system"')
    monkeypatch.chdir(tmp_path)
    staging_cfg = Config.read("staging")
    prod_cfg = Config.read("production")
    assert staging_cfg.effective_secrets(staging_cfg.hosts[0]).adapter == "system"
    prod_host = prod_cfg.select_host("production")
    assert prod_cfg.effective_secrets(prod_host).adapter == "bitwarden"


def test_resolve_secrets_skips_adapter_without_refs(monkeypatch):
    """A host env without $-refs must never contact an adapter (#75)."""
    import fujin.secrets as secrets

    def fake_adapter(env, config):  # pragma: no cover - must never run
        raise AssertionError("adapter must not be called without secret refs")

    monkeypatch.setitem(secrets.get_adapter_registry(), "bitwarden", fake_adapter)
    out = secrets.resolve_secrets(
        'DATABASE_URL="postgres://x"\nDEBUG=True',
        SecretConfig(adapter="bitwarden"),
    )
    assert "DATABASE_URL=postgres://x" in out
    assert "DEBUG=True" in out
