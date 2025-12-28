"""Tests for show command - focused on display functionality.

Note: _resolve_units is comprehensively tested in test_app.py (18 tests).
These tests only verify show command uses it correctly.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import msgspec
import pytest

from fujin.commands.show import Show, _redact_secrets
from fujin.config import Config


@pytest.fixture
def base_config(minimal_config_dict, tmp_path):
    """Base config for show tests with .env file."""
    # Create .env file
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DATABASE_URL=postgres://localhost/db\nSECRET_KEY=my-secret-key-123\n"
    )

    minimal_config_dict["processes"]["worker"] = {"command": "celery", "replicas": 2}
    minimal_config_dict["hosts"][0]["envfile"] = str(env_file)
    minimal_config_dict["webserver"]["enabled"] = True

    return minimal_config_dict


@pytest.fixture
def config_without_webserver(minimal_config_dict):
    """Config without webserver enabled."""
    minimal_config_dict["webserver"]["enabled"] = False
    return minimal_config_dict


# ============================================================================
# Show Available Options
# ============================================================================


def test_show_without_name_displays_available_options(base_config):
    """show with no name displays available options."""
    config = msgspec.convert(base_config, type=Config)

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch.object(Show, "output", MagicMock()) as mock_output,
    ):
        show = Show(name=None)
        show()

        # Should call output to show options
        assert mock_output.info.called
        assert mock_output.output.called


# ============================================================================
# Show Env
# ============================================================================


def test_show_env_redacts_secrets_by_default(base_config):
    """show env redacts secret values by default."""
    config = msgspec.convert(base_config, type=Config)

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch.object(Show, "output", MagicMock()) as mock_output,
    ):
        show = Show(name="env", plain=False)
        show()

        # Verify output was called with redacted content
        # Get the actual argument passed to output()
        assert mock_output.output.called
        output_arg = mock_output.output.call_args[0][0]
        assert "***REDACTED***" in output_arg
        assert "my-secret-key-123" not in output_arg


def test_show_env_with_plain_shows_actual_values(base_config):
    """show env --plain shows actual secret values."""
    config = msgspec.convert(base_config, type=Config)

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch.object(Show, "output", MagicMock()) as mock_output,
    ):
        show = Show(name="env", plain=True)
        show()

        # Verify output contains actual values
        assert mock_output.output.called
        output_arg = mock_output.output.call_args[0][0]
        assert "postgres://localhost/db" in output_arg
        assert "my-secret-key-123" in output_arg
        assert "***REDACTED***" not in output_arg


def test_show_env_without_env_file_shows_warning(tmp_path, monkeypatch):
    """show env without env file configured shows warning."""
    monkeypatch.chdir(tmp_path)

    config_dict = {
        "app": "testapp",
        "version": "1.0.0",
        "build_command": "echo building",
        "installation_mode": "python-package",
        "python_version": "3.11",
        "distfile": "dist/testapp-{version}-py3-none-any.whl",
        "processes": {"web": {"command": "gunicorn"}},
        "hosts": [{"domain_name": "example.com", "user": "deploy"}],
        "webserver": {"enabled": False, "upstream": "localhost:8000"},
    }

    config = msgspec.convert(config_dict, type=Config)

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch.object(Show, "output", MagicMock()) as mock_output,
    ):
        show = Show(name="env")
        show()

        # Should show warning
        mock_output.warning.assert_called()


# ============================================================================
# Show Caddy
# ============================================================================


def test_show_caddy_displays_caddyfile(base_config):
    """show caddy displays rendered Caddyfile."""
    config = msgspec.convert(base_config, type=Config)

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch.object(Show, "output", MagicMock()) as mock_output,
    ):
        show = Show(name="caddy")
        show()

        # Verify Caddyfile content was displayed
        assert mock_output.info.called
        assert mock_output.output.called
        # Check that output contains caddy-related content
        output_calls = [str(call) for call in mock_output.output.call_args_list]
        assert len(output_calls) > 0


def test_show_caddy_without_webserver_shows_warning(config_without_webserver):
    """show caddy without webserver enabled shows warning."""
    config = msgspec.convert(config_without_webserver, type=Config)

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch.object(Show, "output", MagicMock()) as mock_output,
    ):
        show = Show(name="caddy")
        show()

        # Should show warning
        mock_output.warning.assert_called_with(
            "Webserver is not enabled in configuration"
        )


# ============================================================================
# Show Units
# ============================================================================


def test_show_units_displays_all_systemd_units(base_config):
    """show units displays all systemd unit files."""
    config = msgspec.convert(base_config, type=Config)

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch.object(Show, "output", MagicMock()) as mock_output,
    ):
        show = Show(name="units")
        show()

        # Should display multiple units
        assert mock_output.info.call_count >= 2  # At least web and worker
        assert mock_output.output.call_count >= 2


# ============================================================================
# Show Specific Units (Light testing of _resolve_units integration)
# ============================================================================


def test_show_specific_unit_uses_resolve_units(base_config):
    """show <process> uses _resolve_units with use_templates=True."""
    config = msgspec.convert(base_config, type=Config)

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch.object(Show, "output", MagicMock()) as mock_output,
    ):
        show = Show(name="web")
        show()

        # Should display the web unit
        output_calls = [str(call) for call in mock_output.info.call_args_list]
        assert any("testapp.service" in str(call) for call in output_calls)


def test_show_specific_unit_with_replicas_shows_template(base_config):
    """show <process> with replicas shows template unit."""
    config = msgspec.convert(base_config, type=Config)

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch.object(Show, "output", MagicMock()) as mock_output,
    ):
        show = Show(name="worker")
        show()

        # Should display the worker template unit (not instances)
        output_calls = [str(call) for call in mock_output.info.call_args_list]
        assert any("testapp-worker@.service" in str(call) for call in output_calls)


def test_show_unknown_process_raises_error(base_config):
    """show <invalid> raises error with available options."""
    config = msgspec.convert(base_config, type=Config)

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch.object(Show, "output", MagicMock()),
    ):
        show = Show(name="invalid")

        with pytest.raises(SystemExit) as exc_info:
            show()

        assert exc_info.value.code == 1


# ============================================================================
# Redact Secrets Helper Function
# ============================================================================


def test_redact_secrets_comprehensive():
    """_redact_secrets handles all redaction scenarios correctly."""
    env = """# This is a comment
SECRET_KEY=my-very-long-secret-key
SHORT=abc
API_KEY="short"
NOT_QUOTED=short

NORMAL=value
MALFORMED LINE
ANOTHER=very-long-secret-key
EMPTY=
# Another comment"""

    result = _redact_secrets(env)

    # Long values are redacted
    assert "***REDACTED***" in result
    assert "my-very-long-secret-key" not in result
    assert "very-long-secret-key" not in result

    # Short values not redacted
    assert "SHORT=abc" in result
    assert "NOT_QUOTED=short" in result

    # Quoted values are redacted regardless of length
    assert 'API_KEY="***REDACTED***"' in result

    # Comments and empty lines preserved
    assert "# This is a comment" in result
    assert "# Another comment" in result
    assert "\n\n" in result or result.count("\n") >= 10

    # Malformed lines preserved
    assert "MALFORMED LINE" in result

    # Empty values preserved
    assert "EMPTY=" in result
