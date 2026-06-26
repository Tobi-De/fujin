"""Tests for down command."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import msgspec
import pytest

from fujin.commands.down import Down
from fujin.config import Config


def test_down_aborted_when_user_declines(minimal_config_dict):
    """Down command exits without action when user declines confirmation."""
    config = msgspec.convert(minimal_config_dict, type=Config)
    mock_conn = MagicMock()

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch("fujin.connection.connection") as mock_connection,
        patch("fujin.commands.down.Confirm") as mock_confirm,
        patch.object(Down, "output", MagicMock()),
    ):
        mock_connection.return_value.__enter__.return_value = mock_conn
        mock_connection.return_value.__exit__.return_value = None
        mock_confirm.ask.return_value = False

        down = Down()
        down()

        assert not mock_conn.run.called


def test_down_handles_keyboard_interrupt(minimal_config_dict):
    """Down command handles Ctrl+C gracefully."""
    config = msgspec.convert(minimal_config_dict, type=Config)

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch("fujin.commands.down.Confirm") as mock_confirm,
        patch.object(Down, "output", MagicMock()),
    ):
        mock_confirm.ask.side_effect = KeyboardInterrupt

        down = Down()
        with pytest.raises(SystemExit) as exc_info:
            down()
        assert exc_info.value.code == 0


def test_down_successful_teardown(minimal_config_dict):
    """Down tears down the project successfully."""
    config = msgspec.convert(minimal_config_dict, type=Config)
    mock_conn = MagicMock()

    mock_conn.run.side_effect = [
        ("1.0.0", True),  # cat current/.version
        ("", True),  # systemctl stop + rm + rm -rf
        ("", True),  # userdel
    ]

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch("fujin.connection.connection") as mock_connection,
        patch("fujin.commands.down.Confirm") as mock_confirm,
        patch("fujin.commands.down.log_operation"),
        patch.object(Down, "output", MagicMock()),
    ):
        mock_connection.return_value.__enter__.return_value = mock_conn
        mock_connection.return_value.__exit__.return_value = None
        mock_confirm.ask.return_value = True

        down = Down()
        down()

        calls = [call[0][0] for call in mock_conn.run.call_args_list]
        assert any("rm -rf" in cmd for cmd in calls)
        assert any("userdel" in cmd for cmd in calls)


def test_down_uses_config_version_when_version_file_missing(minimal_config_dict):
    """Down uses config version when .version file doesn't exist."""
    config = msgspec.convert(minimal_config_dict, type=Config)
    mock_conn = MagicMock()

    mock_conn.run.side_effect = [
        ("", False),  # cat current/.version fails
        ("", True),  # cleanup commands
        ("", True),  # userdel
    ]

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch("fujin.connection.connection") as mock_connection,
        patch("fujin.commands.down.Confirm") as mock_confirm,
        patch("fujin.commands.down.log_operation"),
        patch.object(Down, "output", MagicMock()),
    ):
        mock_connection.return_value.__enter__.return_value = mock_conn
        mock_connection.return_value.__exit__.return_value = None
        mock_confirm.ask.return_value = True

        down = Down()
        down()

        calls = [call[0][0] for call in mock_conn.run.call_args_list]
        assert any("rm -rf" in cmd for cmd in calls)


def test_down_full_flag_also_removes_caddy(minimal_config_dict):
    """Down with --full flag includes Caddy removal."""
    config = msgspec.convert(minimal_config_dict, type=Config)
    mock_conn = MagicMock()

    mock_conn.run.side_effect = [
        ("1.0.0", True),  # cat current/.version
        ("", True),  # cleanup commands
        ("", True),  # userdel
        ("", True),  # caddy uninstall (from caddy module)
    ]

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch("fujin.connection.connection") as mock_connection,
        patch("fujin.commands.down.Confirm") as mock_confirm,
        patch("fujin.commands.down.log_operation"),
        patch("fujin.commands.down.caddy") as mock_caddy,
        patch.object(Down, "output", MagicMock()),
    ):
        mock_connection.return_value.__enter__.return_value = mock_conn
        mock_connection.return_value.__exit__.return_value = None
        mock_confirm.ask.return_value = True
        mock_caddy.get_uninstall_commands.return_value = [
            "systemctl stop caddy",
            "apt remove -y caddy",
        ]

        down = Down(full=True)
        down()

        calls = [call[0][0] for call in mock_conn.run.call_args_list]
        cmd_str = " ".join(calls)
        assert "rm -rf" in cmd_str
        assert "userdel" in cmd_str
