"""Tests for rollback command - error handling and user interaction.

Full rollback workflows are tested in integration tests.
See tests/integration/test_full_deploy.py
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import msgspec
import pytest

from fujin.commands.rollback import Rollback
from fujin.config import Config

# ============================================================================
# User Interaction
# ============================================================================


def test_rollback_aborts_on_keyboard_interrupt(minimal_config_dict):
    """Rollback handles Ctrl+C gracefully during version selection."""
    config = msgspec.convert(minimal_config_dict, type=Config)
    mock_conn = MagicMock()

    # Combined: cat .version; echo '---'; ls -1t .versions
    mock_conn.run.return_value = (
        "1.0.0\n---\ntestapp-1.0.0.pyz\ntestapp-0.9.0.pyz",
        True,
    )

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch.object(Rollback, "connection") as mock_connection,
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

    # Combined: cat .version; echo '---'; ls -1t .versions
    mock_conn.run.return_value = (
        "1.1.0\n---\ntestapp-1.1.0.pyz\ntestapp-1.0.0.pyz",
        True,
    )

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch.object(Rollback, "connection") as mock_connection,
        patch("fujin.commands.rollback.IntPrompt") as mock_prompt,
        patch("fujin.commands.rollback.Console"),
        patch("fujin.commands.rollback.Confirm") as mock_confirm,
        patch.object(Rollback, "output", MagicMock()),
    ):
        mock_connection.return_value.__enter__.return_value = mock_conn
        mock_connection.return_value.__exit__.return_value = None
        mock_prompt.ask.return_value = 1  # Select first option
        mock_confirm.ask.return_value = False  # User declines

        rollback = Rollback()
        rollback()

        # Should only have called the combined command, not uninstall/install
        assert mock_conn.run.call_count == 1


def test_rollback_strict_fails_when_no_targets_available(minimal_config_dict):
    """Rollback with --strict exits with error when no rollback targets are available."""
    config = msgspec.convert(minimal_config_dict, type=Config)
    mock_conn = MagicMock()
    mock_conn.run.return_value = ("", False)  # Command failed entirely

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch.object(Rollback, "connection") as mock_connection,
        patch.object(Rollback, "output", MagicMock()),
    ):
        mock_connection.return_value.__enter__.return_value = mock_conn
        mock_connection.return_value.__exit__.return_value = None

        rollback = Rollback(strict=True)

        with pytest.raises(SystemExit) as exc_info:
            rollback()

        assert exc_info.value.code == 1


def test_rollback_strict_fails_when_only_current_version_exists(minimal_config_dict):
    """Rollback with --strict exits with error when only the current version exists."""
    config = msgspec.convert(minimal_config_dict, type=Config)
    mock_conn = MagicMock()

    # Combined: cat .version; echo '---'; ls -1t .versions (only current version)
    mock_conn.run.return_value = ("1.0.0\n---\ntestapp-1.0.0.pyz", True)

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch.object(Rollback, "connection") as mock_connection,
        patch.object(Rollback, "output", MagicMock()),
    ):
        mock_connection.return_value.__enter__.return_value = mock_conn
        mock_connection.return_value.__exit__.return_value = None

        rollback = Rollback(strict=True)

        with pytest.raises(SystemExit) as exc_info:
            rollback()

        assert exc_info.value.code == 1


def test_rollback_shows_info_when_no_targets_available(minimal_config_dict):
    """Rollback shows info when no rollback targets are available."""
    config = msgspec.convert(minimal_config_dict, type=Config)
    mock_conn = MagicMock()
    mock_conn.run.return_value = ("", False)  # Command failed entirely

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch.object(Rollback, "connection") as mock_connection,
        patch.object(Rollback, "output", MagicMock()) as mock_output,
    ):
        mock_connection.return_value.__enter__.return_value = mock_conn
        mock_connection.return_value.__exit__.return_value = None

        rollback = Rollback()
        rollback()

        # Should show info message
        mock_output.info.assert_called_with("No rollback targets available")


def test_rollback_shows_info_when_only_current_version_exists(minimal_config_dict):
    """Rollback shows info when only the current version exists (no previous versions)."""
    config = msgspec.convert(minimal_config_dict, type=Config)
    mock_conn = MagicMock()

    # Combined: cat .version; echo '---'; ls -1t .versions (only current version)
    mock_conn.run.return_value = ("1.0.0\n---\ntestapp-1.0.0.pyz", True)

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch.object(Rollback, "connection") as mock_connection,
        patch.object(Rollback, "output", MagicMock()) as mock_output,
    ):
        mock_connection.return_value.__enter__.return_value = mock_conn
        mock_connection.return_value.__exit__.return_value = None

        rollback = Rollback()
        rollback()

        # Should show info message about no previous versions
        mock_output.info.assert_called_with(
            "No previous versions available for rollback"
        )


def test_rollback_previous_flag_auto_selects_most_recent(minimal_config_dict):
    """Rollback with --previous flag auto-selects the most recent previous version."""
    config = msgspec.convert(minimal_config_dict, type=Config)
    mock_conn = MagicMock()

    mock_conn.run.side_effect = [
        # Combined: cat .version; echo '---'; ls -1t .versions
        ("1.1.0\n---\ntestapp-1.1.0.pyz\ntestapp-1.0.0.pyz\ntestapp-0.9.0.pyz", True),
        (None, True),  # test -f (bundle exists)
        ("", True),  # uninstall
        ("", True),  # install + cleanup
    ]

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch.object(Rollback, "connection") as mock_connection,
        patch("fujin.commands.rollback.IntPrompt") as mock_prompt,
        patch("fujin.commands.rollback.Confirm") as mock_confirm,
        patch("fujin.commands.rollback.log_operation"),
        patch.object(Rollback, "output", MagicMock()) as mock_output,
    ):
        mock_connection.return_value.__enter__.return_value = mock_conn
        mock_connection.return_value.__exit__.return_value = None

        rollback = Rollback(previous=True)
        rollback()

        # Should NOT have prompted for version selection
        mock_prompt.ask.assert_not_called()
        mock_confirm.ask.assert_not_called()

        # Should have shown info about rolling back to the most recent previous (1.0.0)
        mock_output.info.assert_any_call("Rolling back from 1.1.0 to 1.0.0...")

        # Should show success message
        mock_output.success.assert_called_with(
            "Rollback to version 1.0.0 completed successfully!"
        )
