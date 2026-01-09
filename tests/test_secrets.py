"""Tests for secrets resolution."""

from __future__ import annotations

import os


from fujin.config import SecretConfig
from fujin.secrets import system


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
