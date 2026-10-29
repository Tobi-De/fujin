"""Tests for the ``fujin-secrets-bitwarden-login`` seeding command."""

from __future__ import annotations

import uuid

import pytest

from fujin.config import SecretConfig
from fujin.errors import SecretResolutionError
from fujin_secrets_bitwarden import login, protocol, store

from fake_server import (
    EMAIL,
    NORMALIZED_EMAIL,
    PASSWORD,
    PASSWORD_ENV,
    PROD,
    REMEMBER_TOKEN,
    FakeServer,
    FakeVault,
    standard_vault,
)

CODE = "123456"


@pytest.fixture
def vault(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(store.STATE_DIR_ENV, str(tmp_path / "state"))
    monkeypatch.setenv(PASSWORD_ENV, PASSWORD)
    fake = standard_vault(FakeVault())
    with FakeServer(fake) as server:
        yield server


def seed_args(vault: FakeVault, *extra: str) -> list[str]:
    return [
        "--email",
        EMAIL,
        "--server-url",
        vault.server_url,
        "--password-env",
        PASSWORD_ENV,
        "--code",
        CODE,
        *extra,
    ]


def state_file(vault: FakeVault):
    return store.path_for(vault.server_url, NORMALIZED_EMAIL)


def config(vault: FakeVault, **overrides) -> SecretConfig:
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


def write_config(
    path, adapter: str = "bitwarden-protocol", email: str = "config@example.com"
):
    (path / "fujin.toml").write_text(
        f"""
app = "demo"
version = "1.0.0"
build_command = "true"
installation_mode = "binary"
distfile = "dist/{{version}}.tar.gz"

[[hosts]]
name = "prod"
address = "example.com"
user = "deploy"

[secrets]
adapter = "{adapter}"

[secrets.options]
email = "{email}"
password_env = "CONFIG_PASSWORD_ENV"
server_url = "http://127.0.0.1:1"
""",
        encoding="utf-8",
    )


def test_seed_once_then_deploy_without_a_code(vault, capsys):
    assert login.main(seed_args(vault)) == 0

    form = vault.token_forms[0]
    assert form["deviceIdentifier"]
    uuid.UUID(form["deviceIdentifier"][0])
    assert form["twoFactorToken"] == [CODE]
    assert form["twoFactorProvider"] == ["0"]
    assert form["twoFactorRemember"] == ["1"]

    stored = store.load(vault.server_url, NORMALIZED_EMAIL)
    assert stored is not None
    assert stored.two_factor_token == REMEMBER_TOKEN
    assert stored.device_identifier == form["deviceIdentifier"][0]
    assert stored.email == NORMALIZED_EMAIL

    output = capsys.readouterr()
    assert str(state_file(vault)) in output.out
    assert CODE not in output.out + output.err
    assert PASSWORD not in output.out + output.err
    assert REMEMBER_TOKEN not in output.out + output.err

    # Deploy: no code, no prompt, same device, provider 5.
    vault.requests.clear()
    vault.token_forms.clear()
    resolved = protocol.bitwarden_protocol("DB_PASSWORD=$DB_PASSWORD", config(vault))
    assert resolved == "DB_PASSWORD=db-secret"

    deploy_form = vault.token_forms[0]
    assert deploy_form["deviceIdentifier"] == form["deviceIdentifier"]
    assert deploy_form["twoFactorToken"] == [REMEMBER_TOKEN]
    assert deploy_form["twoFactorProvider"] == ["5"]
    assert deploy_form["twoFactorRemember"] == ["0"]
    assert CODE not in str(deploy_form)


def test_unseeded_account_fails_closed(vault):
    with pytest.raises(SecretResolutionError) as excinfo:
        protocol.bitwarden_protocol("DB_PASSWORD=$DB_PASSWORD", config(vault))

    message = str(excinfo.value.message)
    assert "fujin-secrets-bitwarden-login" in message
    assert str(state_file(vault)) in message


def test_clear_then_seed_again(vault, capsys):
    assert login.main(seed_args(vault)) == 0
    assert state_file(vault).exists()

    clear_args = [
        "--email",
        EMAIL,
        "--server-url",
        vault.server_url,
        "--clear",
    ]
    assert login.main(clear_args) == 0
    assert not state_file(vault).exists()
    assert "Removed" in capsys.readouterr().out

    with pytest.raises(SecretResolutionError):
        protocol.bitwarden_protocol("DB_PASSWORD=$DB_PASSWORD", config(vault))

    assert login.main(seed_args(vault)) == 0
    assert state_file(vault).exists()
    assert store.load(vault.server_url, NORMALIZED_EMAIL).two_factor_token == (
        REMEMBER_TOKEN
    )


def test_clear_without_stored_device_reports_failure(vault):
    exit_code = login.main(
        ["--email", EMAIL, "--server-url", vault.server_url, "--clear"]
    )
    assert exit_code == 1
    assert not vault.has_token_request()


def test_clear_does_not_need_a_password(vault, monkeypatch):
    monkeypatch.delenv(PASSWORD_ENV, raising=False)

    assert (
        login.main(["--email", EMAIL, "--server-url", vault.server_url, "--clear"]) == 1
    )


def test_provider_flag_is_sent(vault):
    assert login.main(seed_args(vault, "--provider", "7")) == 0
    assert vault.token_forms[0]["twoFactorProvider"] == ["7"]


@pytest.mark.parametrize("value", ["9", "-1", "two"])
def test_invalid_provider_is_rejected(vault, value):
    with pytest.raises(SystemExit) as excinfo:
        login.main(seed_args(vault, "--provider", value))
    assert excinfo.value.code == 2
    assert not vault.has_token_request()


def test_flags_win_over_fujin_toml(vault, tmp_path):
    write_config(tmp_path)

    assert login.main(seed_args(vault, "--host", "prod")) == 0

    assert store.load(vault.server_url, NORMALIZED_EMAIL) is not None
    assert store.load(vault.server_url, "config@example.com") is None
    assert vault.token_forms[0]["username"] == [NORMALIZED_EMAIL]


def test_settings_come_from_fujin_toml_when_flags_are_omitted(vault, tmp_path):
    # The config's server_url would be unreachable; the flag must win, so the
    # run succeeds and the code/password flags are still needed interactively.
    (tmp_path / "fujin.toml").write_text(
        f"""
app = "demo"
version = "1.0.0"
build_command = "true"
installation_mode = "binary"
distfile = "dist/{{version}}.tar.gz"

[[hosts]]
name = "prod"
address = "example.com"
user = "deploy"

[secrets]
adapter = "bitwarden-protocol"

[secrets.options]
email = "{EMAIL}"
password_env = "{PASSWORD_ENV}"
server_url = "{vault.server_url}"
""",
        encoding="utf-8",
    )

    assert (
        login.main(["--host", "prod", "--server-url", vault.server_url, "--code", CODE])
        == 0
    )
    assert store.load(vault.server_url, NORMALIZED_EMAIL) is not None


def test_password_is_prompted_when_no_env_is_configured(vault, monkeypatch):
    prompts = []

    def fake_getpass(prompt):
        prompts.append(prompt)
        return PASSWORD

    monkeypatch.setattr(login.getpass, "getpass", fake_getpass)
    monkeypatch.setattr("builtins.input", lambda prompt: CODE)

    exit_code = login.main(["--email", EMAIL, "--server-url", vault.server_url])

    assert exit_code == 0
    assert prompts == ["Bitwarden master password: "]
    assert store.load(vault.server_url, NORMALIZED_EMAIL) is not None


def test_missing_password_env_is_reported(vault, monkeypatch, capsys):
    monkeypatch.delenv(PASSWORD_ENV, raising=False)

    exit_code = login.main(seed_args(vault))

    assert exit_code == 1
    output = capsys.readouterr()
    assert PASSWORD_ENV in output.err
    assert not vault.has_token_request()


def test_missing_remember_token_fails_and_stores_nothing(vault):
    vault.omit_remember_token = True

    assert login.main(seed_args(vault)) == 1
    assert not state_file(vault).exists()


def test_rejected_code_is_value_free(vault, capsys):
    args = seed_args(vault)
    args[args.index("--code") + 1] = "000000"

    assert login.main(args) == 1

    output = capsys.readouterr()
    assert "000000" not in output.out + output.err
    assert "TwoFactorProviders" not in output.err


def test_different_adapter_is_refused(vault, tmp_path):
    write_config(tmp_path, adapter="bitwarden")

    assert login.main(seed_args(vault)) == 1
    assert not vault.has_token_request()
    assert not state_file(vault).exists()


def test_email_is_required_without_config(vault, capsys):
    assert login.main(["--server-url", vault.server_url]) == 1
    assert "email" in capsys.readouterr().err
    assert not vault.has_token_request()


def test_state_dir_flag_overrides_environment(vault, tmp_path):
    explicit = tmp_path / "elsewhere"

    assert login.main(seed_args(vault, "--state-dir", str(explicit))) == 0

    assert store.load(vault.server_url, NORMALIZED_EMAIL, explicit) is not None
    assert not state_file(vault).exists()
