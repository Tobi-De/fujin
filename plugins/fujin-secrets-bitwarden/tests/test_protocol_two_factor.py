"""Trusted-device (two-factor) behaviour of the protocol adapter."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from fujin.config import SecretConfig
from fujin.errors import SecretResolutionError
from fujin_secrets_bitwarden import protocol, store
from fujin_secrets_bitwarden.store import RememberedDevice

from fake_server import (
    EMAIL,
    NORMALIZED_EMAIL,
    PASSWORD,
    PASSWORD_ENV,
    PROD,
    REMEMBER_TOKEN,
    ROTATED_TOKEN,
    FakeServer,
    FakeVault,
    standard_vault,
)

DEVICE_IDENTIFIER = "seeded-device-identifier"


@pytest.fixture
def state_dir(monkeypatch, tmp_path):
    directory = tmp_path / "state"
    monkeypatch.setenv(store.STATE_DIR_ENV, str(directory))
    return directory


@pytest.fixture
def vault(monkeypatch, state_dir):
    fake = standard_vault(FakeVault())
    monkeypatch.setenv(PASSWORD_ENV, PASSWORD)
    with FakeServer(fake) as server:
        yield server


def secret_config(vault: FakeVault, **overrides) -> SecretConfig:
    options = {
        "email": EMAIL,
        "password_env": PASSWORD_ENV,
        "server_url": vault.server_url,
        "folders": [PROD],
    }
    options.update(overrides)
    return SecretConfig(
        adapter="bitwarden-protocol",
        options={key: value for key, value in options.items() if value is not None},
    )


def save_device(vault: FakeVault, state_dir, **overrides) -> RememberedDevice:
    values = {
        "server_url": vault.server_url,
        "email": NORMALIZED_EMAIL,
        "device_identifier": DEVICE_IDENTIFIER,
        "two_factor_token": REMEMBER_TOKEN,
        "obtained_at": datetime.now(UTC),
    }
    values.update(overrides)
    remember = RememberedDevice(**values)
    store.save(remember, state_dir)
    return remember


def error_message(exc: BaseException) -> str:
    return str(getattr(exc, "message", exc))


def resolve(vault: FakeVault) -> str:
    return protocol.bitwarden_protocol("DB_PASSWORD=$DB_PASSWORD", secret_config(vault))


def test_seeded_device_reuses_token_and_identifier(vault, state_dir):
    save_device(vault, state_dir)

    assert resolve(vault) == "DB_PASSWORD=db-secret"

    form = vault.token_forms[0]
    assert form["deviceIdentifier"] == [DEVICE_IDENTIFIER]
    assert form["twoFactorToken"] == [REMEMBER_TOKEN]
    assert form["twoFactorProvider"] == ["5"]
    assert form["twoFactorRemember"] == ["0"]


def test_remembered_login_keeps_stored_device_untouched(vault, state_dir):
    saved = save_device(vault, state_dir)
    path = store.path_for(vault.server_url, NORMALIZED_EMAIL, state_dir)
    before = path.read_bytes()

    assert resolve(vault) == "DB_PASSWORD=db-secret"

    assert path.read_bytes() == before
    assert store.load(vault.server_url, NORMALIZED_EMAIL, state_dir) == saved


def test_new_remember_token_on_remembered_login_is_persisted(vault, state_dir):
    save_device(vault, state_dir)
    vault.rotate_remember_token = True
    before = datetime.now(UTC)

    assert resolve(vault) == "DB_PASSWORD=db-secret"

    stored = store.load(vault.server_url, NORMALIZED_EMAIL, state_dir)
    assert stored is not None
    assert stored.two_factor_token == ROTATED_TOKEN
    assert stored.device_identifier == DEVICE_IDENTIFIER
    assert stored.obtained_at >= before

    # A second deploy presents the rotated token.
    vault.token_forms.clear()
    vault.remember_token = ROTATED_TOKEN
    assert resolve(vault) == "DB_PASSWORD=db-secret"
    assert vault.token_forms[0]["twoFactorToken"] == [ROTATED_TOKEN]


def test_unseeded_two_factor_account_fails_with_seed_instructions(vault, state_dir):
    with pytest.raises(SecretResolutionError) as excinfo:
        resolve(vault)

    message = error_message(excinfo.value)
    assert "fujin-secrets-bitwarden-login" in message
    assert str(store.path_for(vault.server_url, NORMALIZED_EMAIL)) in message
    assert "TwoFactorProviders" not in message
    assert "two factor required" not in message
    assert REMEMBER_TOKEN not in message
    assert excinfo.value.adapter == "bitwarden-protocol"


def test_expired_token_is_not_sent(vault, state_dir):
    save_device(vault, state_dir, obtained_at=datetime.now(UTC) - timedelta(days=40))

    with pytest.raises(SecretResolutionError) as excinfo:
        resolve(vault)

    assert vault.has_token_request() is False
    assert vault.paths == ["/identity/accounts/prelogin"]
    message = error_message(excinfo.value)
    assert "fujin-secrets-bitwarden-login" in message
    assert str(store.path_for(vault.server_url, NORMALIZED_EMAIL)) in message
    assert REMEMBER_TOKEN not in message


def test_stale_token_rejected_by_server_points_at_reseeding(vault, state_dir):
    save_device(vault, state_dir, two_factor_token="server-forgot-this")

    with pytest.raises(SecretResolutionError) as excinfo:
        resolve(vault)

    message = error_message(excinfo.value)
    assert "fujin-secrets-bitwarden-login" in message
    assert str(store.path_for(vault.server_url, NORMALIZED_EMAIL)) in message
    assert "server-forgot-this" not in message


def test_non_two_factor_account_needs_no_state(vault, state_dir):
    vault.requires_two_factor = False

    assert resolve(vault) == "DB_PASSWORD=db-secret"

    assert "twoFactorToken" not in vault.token_forms[0]
    assert not state_dir.exists()


def test_seeding_grant_shape_and_remember_token(vault):
    client = protocol._Client(vault.server_url)
    kdf = client.prelogin(NORMALIZED_EMAIL)
    master_key = protocol.derive_master_key(PASSWORD, NORMALIZED_EMAIL, kdf)

    result = client.login(
        NORMALIZED_EMAIL,
        protocol.master_password_hash(master_key, PASSWORD),
        device_identifier="seed-device",
        two_factor_code="123456",
        two_factor_provider=1,
        remember=True,
    )

    form = vault.token_forms[0]
    assert form["deviceIdentifier"] == ["seed-device"]
    assert form["twoFactorToken"] == ["123456"]
    assert form["twoFactorProvider"] == ["1"]
    assert form["twoFactorRemember"] == ["1"]
    assert result.remember_token == REMEMBER_TOKEN
    assert result.access_token == vault.access_token


def test_plain_grant_omits_two_factor_fields(vault):
    vault.requires_two_factor = False
    client = protocol._Client(vault.server_url)
    kdf = client.prelogin(NORMALIZED_EMAIL)
    master_key = protocol.derive_master_key(PASSWORD, NORMALIZED_EMAIL, kdf)

    result = client.login(
        NORMALIZED_EMAIL,
        protocol.master_password_hash(master_key, PASSWORD),
        device_identifier="plain-device",
    )

    form = vault.token_forms[0]
    assert form["deviceIdentifier"] == ["plain-device"]
    assert "twoFactorToken" not in form
    assert "twoFactorProvider" not in form
    assert "twoFactorRemember" not in form
    assert result.remember_token is None
