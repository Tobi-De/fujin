"""Tests for the persisted trusted-device store."""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fujin_secrets_bitwarden import store

SERVER = "https://vault.example.com"
EMAIL = "User@Example.com"
NORMALIZED_EMAIL = "user@example.com"


def device(**overrides) -> store.RememberedDevice:
    values = {
        "server_url": SERVER,
        "email": NORMALIZED_EMAIL,
        "device_identifier": "device-identifier",
        "two_factor_token": "remember-token",
        "obtained_at": datetime(2026, 10, 1, 12, 0, tzinfo=UTC),
    }
    values.update(overrides)
    return store.RememberedDevice(**values)


def test_state_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv(store.STATE_DIR_ENV, str(tmp_path / "custom"))
    assert store.default_state_dir() == tmp_path / "custom"

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    assert store.default_state_dir() == tmp_path / "custom"


def test_xdg_state_home_resolution(monkeypatch, tmp_path):
    monkeypatch.delenv(store.STATE_DIR_ENV, raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    assert (
        store.default_state_dir() == tmp_path / "xdg" / "fujin" / "bitwarden-protocol"
    )


def test_home_fallback(monkeypatch, tmp_path):
    monkeypatch.delenv(store.STATE_DIR_ENV, raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert (
        store.default_state_dir()
        == tmp_path / ".local" / "state" / "fujin" / "bitwarden-protocol"
    )


def test_file_name_is_per_account_and_normalized(tmp_path):
    direct = store.path_for(SERVER, EMAIL, tmp_path)
    lower = store.path_for(SERVER, NORMALIZED_EMAIL, tmp_path)
    other_server = store.path_for("https://other.example.com", EMAIL, tmp_path)
    other_email = store.path_for(SERVER, "someone@example.com", tmp_path)

    assert direct == lower
    assert direct.name.startswith("bitwarden-protocol-")
    assert direct.name.endswith(".json")
    digest = direct.stem.removeprefix("bitwarden-protocol-")
    assert len(digest) == 12
    assert len({direct, other_server, other_email}) == 3


def test_save_creates_private_files_and_round_trips(tmp_path):
    state = tmp_path / "state"
    saved = store.save(device(), state)

    assert saved == store.path_for(SERVER, EMAIL, state)
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert stat.S_IMODE(saved.stat().st_mode) == 0o600

    raw = json.loads(saved.read_text())
    assert raw["server_url"] == SERVER
    assert raw["email"] == NORMALIZED_EMAIL
    assert raw["device_identifier"] == "device-identifier"
    assert raw["two_factor_token"] == "remember-token"
    assert raw["obtained_at"].endswith("Z")
    assert set(raw) == {
        "server_url",
        "email",
        "device_identifier",
        "two_factor_token",
        "obtained_at",
    }

    loaded = store.load(SERVER, EMAIL, state)
    assert loaded == device()


def test_save_replaces_atomically_without_leftover_temp_files(tmp_path):
    state = tmp_path / "state"
    store.save(device(two_factor_token="first"), state)
    store.save(device(two_factor_token="second"), state)

    assert store.load(SERVER, EMAIL, state).two_factor_token == "second"
    assert [entry.name for entry in state.iterdir()] == [
        store.path_for(SERVER, EMAIL, state).name
    ]


@pytest.mark.parametrize(
    "content",
    [
        b"not json",
        b"[]",
        b'"string"',
        b"null",
        b'{"server_url": "https://vault.example.com"}',
        b'{"server_url": "x", "email": "e", "device_identifier": "d", '
        b'"two_factor_token": "", "obtained_at": "2026-01-01T00:00:00Z"}',
        b'{"server_url": "x", "email": "e", "device_identifier": "d", '
        b'"two_factor_token": "t", "obtained_at": "yesterday"}',
        b'{"server_url": 1, "email": "e", "device_identifier": "d", '
        b'"two_factor_token": "t", "obtained_at": "2026-01-01T00:00:00Z"}',
    ],
)
def test_corrupt_or_incomplete_state_is_absent(tmp_path, content):
    path = store.path_for(SERVER, EMAIL, tmp_path)
    path.write_bytes(content)

    assert store.load(SERVER, EMAIL, tmp_path) is None


def test_missing_state_is_absent_and_clear_reports_it(tmp_path):
    assert store.load(SERVER, EMAIL, tmp_path) is None
    assert store.clear(SERVER, EMAIL, tmp_path) is False


def test_accounts_are_isolated(tmp_path):
    other = device(
        email="other@example.com",
        device_identifier="other-device",
        two_factor_token="other-token",
    )
    store.save(device(), tmp_path)
    store.save(other, tmp_path)

    assert store.load(SERVER, EMAIL, tmp_path).two_factor_token == "remember-token"
    assert (
        store.load(SERVER, "other@example.com", tmp_path).two_factor_token
        == "other-token"
    )

    assert store.clear(SERVER, EMAIL, tmp_path) is True
    assert store.load(SERVER, EMAIL, tmp_path) is None
    assert store.load(SERVER, "other@example.com", tmp_path) is not None


def test_unreadable_state_is_absent(tmp_path, monkeypatch):
    store.save(device(), tmp_path)

    def boom(*args, **kwargs):
        raise OSError("no")

    monkeypatch.setattr(Path, "read_text", boom)
    assert store.load(SERVER, EMAIL, tmp_path) is None


def test_expiry_after_29_days():
    fresh = device(obtained_at=datetime.now(UTC) - timedelta(days=28))
    stale = device(obtained_at=datetime.now(UTC) - timedelta(days=30))

    assert fresh.is_expired() is False
    assert stale.is_expired() is True


def test_expiry_accepts_naive_timestamps():
    naive = device(obtained_at=datetime.now() - timedelta(days=30))
    assert naive.is_expired() is True


def test_save_does_not_ignore_umask(tmp_path):
    old = os.umask(0o000)
    try:
        path = store.save(device(), tmp_path / "state")
    finally:
        os.umask(old)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
