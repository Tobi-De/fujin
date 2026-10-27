"""Tests for secrets resolution."""

from __future__ import annotations

import os

import pytest

from fujin.config import SecretConfig
from fujin.errors import SecretResolutionError
from fujin.formatting import safe_format
from fujin.secrets import (
    env_values,
    parse_env_template,
    resolve_secrets,
    system,
)


def test_system_adapter_preserves_simple_values():
    """Simple values without special characters should not be quoted."""
    env_content = "DEBUG=true\nPORT=8000\nENV=production"
    config = SecretConfig(adapter="system")

    result = system(env_content, config)

    assert "DEBUG=true" in result
    assert "PORT=8000" in result
    assert "ENV=production" in result
    # Should not have quotes
    assert 'DEBUG="true"' not in result
    assert 'PORT="8000"' not in result


def test_system_adapter_quotes_values_with_spaces():
    """Values with spaces should be quoted."""
    env_content = "MESSAGE=hello world\nTITLE=My App"
    config = SecretConfig(adapter="system")

    result = system(env_content, config)

    assert 'MESSAGE="hello world"' in result
    assert 'TITLE="My App"' in result


def test_system_adapter_quotes_values_with_special_chars():
    """Values with shell special characters should be quoted."""
    env_content = "CMD=echo test;ls\nVAL=a&b\nPIPE=a|b"
    config = SecretConfig(adapter="system")

    result = system(env_content, config)

    # All should be quoted due to special shell characters
    assert 'CMD="echo test;ls"' in result or "CMD='echo test;ls'" in result
    assert 'VAL="a&b"' in result
    assert 'PIPE="a|b"' in result


def test_system_adapter_allows_common_safe_chars():
    """Common safe characters like :/-_. should not trigger quoting."""
    env_content = (
        "PATH=/usr/bin:/usr/local/bin\nURL=http://example.com\nFILE=test_file.txt"
    )
    config = SecretConfig(adapter="system")

    result = system(env_content, config)

    # These should not be quoted
    assert "PATH=/usr/bin:/usr/local/bin" in result
    assert "URL=http://example.com" in result or 'URL="http://example.com"' in result
    assert "FILE=test_file.txt" in result


def test_system_adapter_resolves_env_var_secrets():
    """Values starting with $ should be resolved from environment."""
    os.environ["MY_SECRET"] = "secret_value"
    env_content = "API_KEY=$MY_SECRET\nSTATIC=not_a_secret"
    config = SecretConfig(adapter="system")

    try:
        result = system(env_content, config)

        assert "API_KEY=secret_value" in result
        assert "STATIC=not_a_secret" in result
        # The secret value should not have quotes if it doesn't need them
        assert "API_KEY=secret_value" in result or 'API_KEY="secret_value"' in result
    finally:
        del os.environ["MY_SECRET"]


def test_system_adapter_handles_empty_values():
    """Empty values should be preserved."""
    env_content = "EMPTY=\nVALUE=something"
    config = SecretConfig(adapter="system")

    result = system(env_content, config)

    assert "EMPTY=" in result
    assert "VALUE=something" in result


# =============================================================================================
# Template parsing: `$$` escapes are literals, `$REF` is a secret reference (#85)
# =============================================================================================


def test_parse_env_template_distinguishes_refs_and_escapes():
    entries = parse_env_template("A=$REF\nB=$$REF\nC=pa$$word\nD=$$")
    parsed = {entry.key: (entry.literal, entry.ref) for entry in entries}
    assert parsed == {
        "A": (None, "REF"),
        "B": ("$REF", None),
        "C": ("pa$word", None),
        "D": ("$", None),
    }


def test_parse_env_template_double_escape_is_one_literal_dollar():
    entries = {entry.key: entry.literal for entry in parse_env_template("A=$$$$")}
    assert entries == {"A": "$$"}


def test_dollar_ref_still_resolves():
    os.environ["MY_SECRET"] = "secret_value"
    try:
        result = system("API_KEY=$MY_SECRET", SecretConfig(adapter="system"))
    finally:
        del os.environ["MY_SECRET"]
    assert env_values(result) == {"API_KEY": "secret_value"}


def test_escaped_ref_is_literal_and_not_looked_up(monkeypatch):
    """`$$REF` must never reach the adapter/getenv path."""
    looked_up: list[str] = []
    real_getenv = os.getenv

    def spy(name, default=None):
        looked_up.append(name)
        return real_getenv(name, default)

    os.environ["REAL_REF"] = "resolved"
    monkeypatch.setattr(os, "getenv", spy)
    try:
        result = system("A=$REAL_REF\nB=$$ESCAPED_REF", SecretConfig(adapter="system"))
    finally:
        del os.environ["REAL_REF"]

    assert "ESCAPED_REF" not in looked_up
    values = env_values(result)
    assert values["A"] == "resolved"
    assert values["B"] == "$ESCAPED_REF"


def test_escaped_ref_does_not_contact_configured_adapter(monkeypatch):
    calls: list[str] = []

    def spy(env_content, secret_config):
        calls.append(env_content)
        return env_content

    monkeypatch.setattr("fujin.secrets.get_adapter_registry", lambda: {"test": spy})
    out = resolve_secrets("A=$$REF\nB=plain", SecretConfig(adapter="test"))

    assert calls == []
    assert env_values(out) == {"A": "$REF", "B": "plain"}


def test_embedded_escape_unescapes_globally():
    result = system("PASSWORD=pa$$word", SecretConfig(adapter="system"))
    assert env_values(result) == {"PASSWORD": "pa$word"}


def test_lone_escape_renders_single_dollar():
    result = system("DOLLAR=$$", SecretConfig(adapter="system"))
    assert env_values(result) == {"DOLLAR": "$"}


def test_undefined_ref_fails_closed_naming_only_the_ref(monkeypatch):
    monkeypatch.delenv("UNDEFINED_REF_XYZ", raising=False)
    with pytest.raises(SecretResolutionError) as excinfo:
        system("TOKEN=$UNDEFINED_REF_XYZ", SecretConfig(adapter="system"))

    message = excinfo.value.message
    assert "UNDEFINED_REF_XYZ" in message
    assert "TOKEN" in message


def test_dotenv_interpolation_preserved_and_escapes_stay_literal(monkeypatch):
    """Back-compat: without `$$`, `${VAR}` still expands exactly as
    python-dotenv does; `$$` opts a dollar out of interpolation."""
    monkeypatch.setenv("HOME", "/home/someone-else")
    out = resolve_secrets(
        "PATHX=/prefix:${HOME}/bin\nESCAPED=$${HOME}\nPASSWORD=pa$$word",
        SecretConfig(adapter="system"),
    )

    assert "PATHX=/prefix:/home/someone-else/bin" in out
    assert 'ESCAPED="${HOME}"' in out
    assert 'PASSWORD="pa$word"' in out


def test_escaped_dollar_next_to_template_placeholder_formats_after_resolution():
    rendered = resolve_secrets(
        "URL=$${app_name}.example", SecretConfig(adapter="system")
    )
    formatted, unresolved = safe_format(rendered, app_name="myapp")

    assert unresolved == set()
    assert env_values(formatted) == {"URL": "$myapp.example"}


def test_literal_only_file_renders_exactly_as_before():
    content = (
        "# comment is dropped\n"
        "DEBUG=true\n"
        'MESSAGE="hello world"\n'
        "PATH=/usr/bin:/bin\n"
        "EMPTY=\n"
        "URL=http://example.com"
    )

    assert resolve_secrets(content, SecretConfig(adapter="system")) == (
        "DEBUG=true\n"
        'MESSAGE="hello world"\n'
        "PATH=/usr/bin:/bin\n"
        "EMPTY=\n"
        "URL=http://example.com"
    )
