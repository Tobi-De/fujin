"""Tests for rollback command."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import msgspec
import pytest

from fujin.commands.rollback import Rollback
from fujin.config import Config


@pytest.fixture
def base_config(tmp_path, monkeypatch):
    """Base config for rollback tests."""
    monkeypatch.chdir(tmp_path)


# ============================================================================
# No Rollback Targets
# ============================================================================


@pytest.mark.parametrize(
    "run_return",
    [
        ("", False),  # No versions directory
        ("README.md\nother-app-1.0.0.pyz", True),  # No matching bundles
    ],
)
def test_rollback_no_targets_available(minimal_config_dict, run_return):
    """rollback shows info when no rollback targets are available."""
    config = msgspec.convert(minimal_config_dict, type=Config)
    mock_conn = MagicMock()
    mock_conn.run.return_value = run_return

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


# ============================================================================
# Version Already Current
# ============================================================================


def test_rollback_to_current_version_shows_warning(minimal_config_dict):
    """rollback to current version shows warning and aborts."""
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


# ============================================================================
# User Interaction
# ============================================================================


def test_rollback_aborts_on_keyboard_interrupt(minimal_config_dict):
    """rollback handles Ctrl+C gracefully during version selection."""
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
    """rollback aborts when user declines confirmation."""
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


# ============================================================================
# Successful Rollback
# ============================================================================


def test_rollback_successful_with_current_bundle_exists(minimal_config_dict):
    """rollback successfully uninstalls current and installs target."""
    config = msgspec.convert(minimal_config_dict, type=Config)
    mock_conn = MagicMock()

    mock_conn.run.side_effect = [
        ("testapp-1.1.0.pyz\ntestapp-1.0.0.pyz", True),  # ls -1t
        ("1.1.0", True),  # cat .version
        ("", True),  # test -f current bundle (exists)
        ("", True),  # uninstall command
        ("", True),  # install + cleanup command
    ]

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch.object(Rollback, "connection") as mock_connection,
        patch("fujin.commands.rollback.Prompt") as mock_prompt,
        patch("fujin.commands.rollback.Confirm") as mock_confirm,
        patch("fujin.commands.rollback.log_operation"),
        patch.object(Rollback, "output", MagicMock()) as mock_output,
    ):
        mock_connection.return_value.__enter__.return_value = mock_conn
        mock_connection.return_value.__exit__.return_value = None
        mock_prompt.ask.return_value = "1.0.0"
        mock_confirm.ask.return_value = True

        rollback = Rollback()
        rollback()

        # Verify uninstall was called
        calls = [call[0][0] for call in mock_conn.run.call_args_list]
        assert any("uninstall" in cmd for cmd in calls)

        # Verify install was called
        assert any("install" in cmd for cmd in calls)

        # Verify success message
        mock_output.success.assert_called_with(
            "Rollback to version 1.0.0 completed successfully!"
        )


@pytest.mark.parametrize(
    "run_side_effect,expected_warning",
    [
        # Current bundle not found
        (
            [
                ("testapp-1.1.0.pyz\ntestapp-1.0.0.pyz", True),
                ("1.1.0", True),
                ("", False),  # test -f fails
                ("", True),
            ],
            "Bundle for current version",
        ),
        # Uninstall fails
        (
            [
                ("testapp-1.1.0.pyz\ntestapp-1.0.0.pyz", True),
                ("1.1.0", True),
                ("", True),  # test -f succeeds
                ("", False),  # uninstall fails
                ("", True),
            ],
            "uninstall failed",
        ),
    ],
)
def test_rollback_continues_on_failure(
    minimal_config_dict, run_side_effect, expected_warning
):
    """rollback continues and completes successfully despite failures."""
    config = msgspec.convert(minimal_config_dict, type=Config)
    mock_conn = MagicMock()
    mock_conn.run.side_effect = run_side_effect

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch.object(Rollback, "connection") as mock_connection,
        patch("fujin.commands.rollback.Prompt") as mock_prompt,
        patch("fujin.commands.rollback.Confirm") as mock_confirm,
        patch("fujin.commands.rollback.log_operation"),
        patch.object(Rollback, "output", MagicMock()) as mock_output,
    ):
        mock_connection.return_value.__enter__.return_value = mock_conn
        mock_connection.return_value.__exit__.return_value = None
        mock_prompt.ask.return_value = "1.0.0"
        mock_confirm.ask.return_value = True

        rollback = Rollback()
        rollback()

        # Should show warning
        assert any(
            expected_warning in str(call) for call in mock_output.warning.call_args_list
        )

        # Should still complete successfully
        mock_output.success.assert_called()


# ============================================================================
# Version Selection and Cleanup
# ============================================================================


def test_rollback_prompts_with_available_versions(minimal_config_dict):
    """rollback prompts with list of available versions."""
    config = msgspec.convert(minimal_config_dict, type=Config)
    mock_conn = MagicMock()

    versions_output = (
        "testapp-1.2.0.pyz\ntestapp-1.1.0.pyz\ntestapp-1.0.0.pyz\ntestapp-0.9.0.pyz"
    )

    mock_conn.run.side_effect = [
        (versions_output, True),  # ls -1t
        ("1.2.0", True),  # cat .version
        ("", True),  # test -f
        ("", True),  # uninstall
        ("", True),  # install + cleanup
    ]

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch.object(Rollback, "connection") as mock_connection,
        patch("fujin.commands.rollback.Prompt") as mock_prompt,
        patch("fujin.commands.rollback.Confirm") as mock_confirm,
        patch("fujin.commands.rollback.log_operation"),
        patch.object(Rollback, "output", MagicMock()),
    ):
        mock_connection.return_value.__enter__.return_value = mock_conn
        mock_connection.return_value.__exit__.return_value = None
        mock_prompt.ask.return_value = "1.0.0"
        mock_confirm.ask.return_value = True

        rollback = Rollback()
        rollback()

        # Verify prompt was called with correct versions
        mock_prompt.ask.assert_called_once()
        call_kwargs = mock_prompt.ask.call_args[1]
        assert call_kwargs["choices"] == ["1.2.0", "1.1.0", "1.0.0", "0.9.0"]
        assert call_kwargs["default"] == "1.2.0"  # Most recent


def test_rollback_cleans_up_newer_versions(minimal_config_dict):
    """rollback cleans up versions newer than target."""
    config = msgspec.convert(minimal_config_dict, type=Config)
    mock_conn = MagicMock()

    mock_conn.run.side_effect = [
        ("testapp-1.2.0.pyz\ntestapp-1.0.0.pyz", True),  # ls -1t
        ("1.2.0", True),  # cat .version
        ("", True),  # test -f
        ("", True),  # uninstall
        ("", True),  # install + cleanup
    ]

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch.object(Rollback, "connection") as mock_connection,
        patch("fujin.commands.rollback.Prompt") as mock_prompt,
        patch("fujin.commands.rollback.Confirm") as mock_confirm,
        patch("fujin.commands.rollback.log_operation"),
        patch.object(Rollback, "output", MagicMock()),
    ):
        mock_connection.return_value.__enter__.return_value = mock_conn
        mock_connection.return_value.__exit__.return_value = None
        mock_prompt.ask.return_value = "1.0.0"
        mock_confirm.ask.return_value = True

        rollback = Rollback()
        rollback()

        # Verify cleanup command was included
        calls = [call[0][0] for call in mock_conn.run.call_args_list]
        # Find the install command (not uninstall)
        install_cmd = [
            cmd for cmd in calls if "install" in cmd and "uninstall" not in cmd
        ][0]

        # Should include cleanup logic
        assert "Cleaning up newer versions" in install_cmd
        assert "xargs -r rm" in install_cmd
