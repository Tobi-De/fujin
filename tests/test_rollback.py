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

    mock_conn.run.return_value = ("testapp-1.0.0.pyz\ntestapp-0.9.0.pyz", True)

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch.object(Rollback, "connection") as mock_connection,
        patch("fujin.commands.rollback.Prompt") as mock_prompt,
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

    mock_conn.run.side_effect = [
        ("testapp-1.1.0.pyz\ntestapp-1.0.0.pyz", True),  # ls -1t
        ("1.1.0", True),  # cat .version
    ]

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch.object(Rollback, "connection") as mock_connection,
        patch("fujin.commands.rollback.Prompt") as mock_prompt,
        patch("fujin.commands.rollback.Confirm") as mock_confirm,
        patch.object(Rollback, "output", MagicMock()),
    ):
        mock_connection.return_value.__enter__.return_value = mock_conn
        mock_connection.return_value.__exit__.return_value = None
        mock_prompt.ask.return_value = "1.0.0"
        mock_confirm.ask.return_value = False  # User declines

        rollback = Rollback()
        rollback()

        # Should only have called ls and cat, not uninstall/install
        assert mock_conn.run.call_count == 2


def test_rollback_shows_info_when_no_targets_available(minimal_config_dict):
    """Rollback shows info when no rollback targets are available."""
    config = msgspec.convert(minimal_config_dict, type=Config)
    mock_conn = MagicMock()
    mock_conn.run.return_value = ("", False)  # No versions directory

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


def test_rollback_warns_when_selecting_current_version(minimal_config_dict):
    """Rollback warns when selecting the already-current version."""
    config = msgspec.convert(minimal_config_dict, type=Config)
    mock_conn = MagicMock()

    mock_conn.run.side_effect = [
        ("testapp-1.0.0.pyz\ntestapp-0.9.0.pyz", True),  # ls -1t
        ("1.0.0", True),  # cat .version
    ]

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch.object(Rollback, "connection") as mock_connection,
        patch("fujin.commands.rollback.Prompt") as mock_prompt,
        patch.object(Rollback, "output", MagicMock()) as mock_output,
    ):
        mock_connection.return_value.__enter__.return_value = mock_conn
        mock_connection.return_value.__exit__.return_value = None
        mock_prompt.ask.return_value = "1.0.0"  # User selects current version

        rollback = Rollback()
        rollback()

        # Should show warning
        mock_output.warning.assert_called_with(
            "Version 1.0.0 is already the current version."
        )
