"""Tests for rollback command."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import msgspec
import pytest

from fujin.commands.rollback import Rollback
from fujin.config import Config


def test_rollback_no_previous_versions(minimal_config_dict):
    """Rollback shows info when no previous versions exist."""
    config = msgspec.convert(minimal_config_dict, type=Config)
    mock_conn = MagicMock()

    # Only current version in releases/
    mock_conn.run.return_value = (
        "1.0.0\n---\n1.0.0",
        True,
    )

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch("fujin.connection.connection") as mock_connection,
        patch.object(Rollback, "output", MagicMock()) as mock_output,
    ):
        mock_connection.return_value.__enter__.return_value = mock_conn
        mock_connection.return_value.__exit__.return_value = None

        rollback = Rollback()
        rollback()

        mock_output.info.assert_called_with(
            "No previous versions available for rollback"
        )


def test_rollback_strict_mode_no_versions(minimal_config_dict):
    """Rollback in strict mode exits with error when no previous versions."""
    config = msgspec.convert(minimal_config_dict, type=Config)
    mock_conn = MagicMock()

    mock_conn.run.return_value = (
        "1.0.0\n---\n1.0.0",
        True,
    )

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch("fujin.connection.connection") as mock_connection,
        patch.object(Rollback, "output", MagicMock()),
    ):
        mock_connection.return_value.__enter__.return_value = mock_conn
        mock_connection.return_value.__exit__.return_value = None

        rollback = Rollback(strict=True)
        with pytest.raises(SystemExit) as exc:
            rollback()
        assert exc.value.code == 1


def test_rollback_previous_flag_auto_selects_most_recent(minimal_config_dict):
    """Rollback with --previous flag auto-selects the most recent previous version."""
    config = msgspec.convert(minimal_config_dict, type=Config)
    mock_conn = MagicMock()

    # Current: 1.1.0, available releases: 1.1.0, 1.0.0, 0.9.0
    mock_conn.run.side_effect = [
        # Combined: readlink + ls -1t releases/
        ("1.1.0\n---\n1.1.0\n1.0.0\n0.9.0", True),
        # test -d releases/1.0.0
        ("", True),
        # ln -sfn + mv (atomic swap)
        ("", True),
        # systemctl restart
        ("", True),
        # Cleanup newer releases
        ("", True),
    ]

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch("fujin.connection.connection") as mock_connection,
        patch("fujin.commands.rollback.IntPrompt") as mock_prompt,
        patch("fujin.commands.rollback.Confirm") as mock_confirm,
        patch("fujin.commands.rollback.log_operation"),
        patch.object(Rollback, "output", MagicMock()) as mock_output,
    ):
        mock_connection.return_value.__enter__.return_value = mock_conn
        mock_connection.return_value.__exit__.return_value = None

        rollback = Rollback(previous=True)
        rollback()

        mock_prompt.ask.assert_not_called()
        mock_confirm.ask.assert_not_called()
        mock_output.info.assert_any_call("Rolling back from 1.1.0 to 1.0.0...")
        mock_output.success.assert_called_with(
            "Rollback to version 1.0.0 completed successfully!"
        )


def test_rollback_error_when_release_missing(minimal_config_dict):
    """Rollback errors when selected release directory is missing."""
    config = msgspec.convert(minimal_config_dict, type=Config)
    mock_conn = MagicMock()

    mock_conn.run.side_effect = [
        ("1.1.0\n---\n1.1.0\n1.0.0", True),  # readlink + ls
        ("", False),  # test -d (release missing!)
    ]

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch("fujin.connection.connection") as mock_connection,
        patch.object(Rollback, "output", MagicMock()) as mock_output,
    ):
        mock_connection.return_value.__enter__.return_value = mock_conn
        mock_connection.return_value.__exit__.return_value = None

        rollback = Rollback(previous=True)
        rollback()

        mock_output.error.assert_called()
        args = mock_output.error.call_args[0][0]
        assert "not found" in args


def test_rollback_aborts_on_keyboard_interrupt(minimal_config_dict):
    """Rollback handles Ctrl+C gracefully during version selection."""
    config = msgspec.convert(minimal_config_dict, type=Config)
    mock_conn = MagicMock()

    mock_conn.run.return_value = (
        "1.0.0\n---\n1.0.0\n0.9.0",
        True,
    )

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch("fujin.connection.connection") as mock_connection,
        patch("fujin.commands.rollback.IntPrompt") as mock_prompt,
        patch("fujin.commands.rollback.Console"),
        patch.object(Rollback, "output", MagicMock()),
    ):
        mock_connection.return_value.__enter__.return_value = mock_conn
        mock_connection.return_value.__exit__.return_value = None
        mock_prompt.ask.side_effect = KeyboardInterrupt

        rollback = Rollback()
        with pytest.raises(SystemExit) as exc_info:
            rollback()
        assert exc_info.value.code == 0


def test_rollback_aborts_when_user_declines_confirmation(minimal_config_dict):
    """Rollback aborts when user declines confirmation."""
    config = msgspec.convert(minimal_config_dict, type=Config)
    mock_conn = MagicMock()

    mock_conn.run.return_value = (
        "1.1.0\n---\n1.1.0\n1.0.0",
        True,
    )

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch("fujin.connection.connection") as mock_connection,
        patch("fujin.commands.rollback.IntPrompt") as mock_prompt,
        patch("fujin.commands.rollback.Confirm") as mock_confirm,
        patch("fujin.commands.rollback.Console"),
        patch.object(Rollback, "output", MagicMock()),
    ):
        mock_connection.return_value.__enter__.return_value = mock_conn
        mock_connection.return_value.__exit__.return_value = None
        mock_prompt.ask.return_value = 1  # Select first option
        mock_confirm.ask.return_value = False  # But decline confirmation

        rollback = Rollback()
        rollback()

        mock_confirm.ask.assert_called_once()
