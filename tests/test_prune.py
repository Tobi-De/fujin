"""Tests for prune command."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import msgspec
import pytest

from fujin.commands.prune import Prune
from fujin.config import Config


# base_config fixture from conftest.py


# ============================================================================
# Validation
# ============================================================================


def test_prune_with_keep_less_than_1_raises_error(minimal_config_dict):
    """prune with --keep < 1 raises error."""
    config = msgspec.convert(minimal_config_dict, type=Config)

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch.object(Prune, "output", MagicMock()),
    ):
        prune = Prune(keep=0)

        with pytest.raises(SystemExit) as exc_info:
            prune()

        assert exc_info.value.code == 1


# ============================================================================
# No Versions Scenarios
# ============================================================================


@pytest.mark.parametrize(
    "run_side_effects,keep,expected_message",
    [
        # No versions directory
        (
            [("", False)],
            2,
            "No versions directory found. Nothing to prune.",
        ),
        # Empty versions directory
        (
            [("", True), ("", True)],
            2,
            "No versions found to prune",
        ),
        # Fewer versions than keep
        (
            [("", True), ("testapp-1.0.0.pyz\ntestapp-0.9.0.pyz", True)],
            3,
            "Only 2 versions found. Nothing to prune (keep=3).",
        ),
    ],
)
def test_prune_no_versions_scenarios(
    minimal_config_dict, run_side_effects, keep, expected_message
):
    """prune handles various no-versions scenarios correctly."""
    config = msgspec.convert(minimal_config_dict, type=Config)
    mock_conn = MagicMock()
    mock_conn.run.side_effect = run_side_effects

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch.object(Prune, "connection") as mock_connection,
        patch.object(Prune, "output", MagicMock()) as mock_output,
    ):
        mock_connection.return_value.__enter__.return_value = mock_conn
        mock_connection.return_value.__exit__.return_value = None

        prune = Prune(keep=keep)
        prune()

        # Should show info message
        mock_output.info.assert_called_with(expected_message)


# ============================================================================
# Successful Pruning
# ============================================================================


def test_prune_deletes_old_versions_when_user_confirms(minimal_config_dict):
    """prune deletes old versions when user confirms."""
    config = msgspec.convert(minimal_config_dict, type=Config)
    mock_conn = MagicMock()

    # 4 versions, newest first (ls -1t sorts by time)
    versions_output = (
        "testapp-1.3.0.pyz\ntestapp-1.2.0.pyz\ntestapp-1.1.0.pyz\ntestapp-1.0.0.pyz"
    )

    mock_conn.run.side_effect = [
        ("", True),  # test -d
        (versions_output, True),  # ls -1t
        ("", True),  # rm command
    ]

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch.object(Prune, "connection") as mock_connection,
        patch("fujin.commands.prune.Confirm") as mock_confirm,
        patch.object(Prune, "output", MagicMock()) as mock_output,
    ):
        mock_connection.return_value.__enter__.return_value = mock_conn
        mock_connection.return_value.__exit__.return_value = None
        mock_confirm.ask.return_value = True

        prune = Prune(keep=2)
        prune()

        # Verify deletion command was called
        calls = [call[0][0] for call in mock_conn.run.call_args_list]
        rm_cmd = [cmd for cmd in calls if "rm -f" in cmd][0]

        # Should keep newest 2 (1.3.0, 1.2.0), delete oldest 2 (1.1.0, 1.0.0)
        assert "testapp-1.1.0.pyz" in rm_cmd
        assert "testapp-1.0.0.pyz" in rm_cmd
        assert "testapp-1.3.0.pyz" not in rm_cmd
        assert "testapp-1.2.0.pyz" not in rm_cmd

        # Should show success message
        mock_output.success.assert_called()


def test_prune_aborts_when_user_declines(minimal_config_dict):
    """prune aborts when user declines confirmation."""
    config = msgspec.convert(minimal_config_dict, type=Config)
    mock_conn = MagicMock()

    versions_output = (
        "testapp-1.3.0.pyz\ntestapp-1.2.0.pyz\ntestapp-1.1.0.pyz\ntestapp-1.0.0.pyz"
    )

    mock_conn.run.side_effect = [
        ("", True),  # test -d
        (versions_output, True),  # ls -1t
    ]

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch.object(Prune, "connection") as mock_connection,
        patch("fujin.commands.prune.Confirm") as mock_confirm,
        patch.object(Prune, "output", MagicMock()) as mock_output,
    ):
        mock_connection.return_value.__enter__.return_value = mock_conn
        mock_connection.return_value.__exit__.return_value = None
        mock_confirm.ask.return_value = False  # User declines

        prune = Prune(keep=2)
        prune()

        # Should not call rm command (only 2 run calls: test -d and ls)
        assert mock_conn.run.call_count == 2

        # Should not show success message
        assert not mock_output.success.called


# ============================================================================
# Keep Parameter Behavior
# ============================================================================


def test_prune_with_keep_1_deletes_all_but_newest(minimal_config_dict):
    """prune with --keep 1 deletes all but the newest version."""
    config = msgspec.convert(minimal_config_dict, type=Config)
    mock_conn = MagicMock()

    versions_output = "testapp-2.0.0.pyz\ntestapp-1.0.0.pyz\ntestapp-0.9.0.pyz"

    mock_conn.run.side_effect = [
        ("", True),  # test -d
        (versions_output, True),  # ls -1t
        ("", True),  # rm command
    ]

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch.object(Prune, "connection") as mock_connection,
        patch("fujin.commands.prune.Confirm") as mock_confirm,
        patch.object(Prune, "output", MagicMock()),
    ):
        mock_connection.return_value.__enter__.return_value = mock_conn
        mock_connection.return_value.__exit__.return_value = None
        mock_confirm.ask.return_value = True

        prune = Prune(keep=1)
        prune()

        # Verify deletion command
        calls = [call[0][0] for call in mock_conn.run.call_args_list]
        rm_cmd = [cmd for cmd in calls if "rm -f" in cmd][0]

        # Should keep only newest (2.0.0), delete others
        assert "testapp-1.0.0.pyz" in rm_cmd
        assert "testapp-0.9.0.pyz" in rm_cmd
        assert "testapp-2.0.0.pyz" not in rm_cmd


def test_prune_with_keep_5_keeps_all_if_only_3_versions(minimal_config_dict):
    """prune with --keep 5 keeps all versions if only 3 exist."""
    config = msgspec.convert(minimal_config_dict, type=Config)
    mock_conn = MagicMock()

    versions_output = "testapp-1.2.0.pyz\ntestapp-1.1.0.pyz\ntestapp-1.0.0.pyz"

    mock_conn.run.side_effect = [
        ("", True),  # test -d
        (versions_output, True),  # ls -1t
    ]

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch.object(Prune, "connection") as mock_connection,
        patch.object(Prune, "output", MagicMock()) as mock_output,
    ):
        mock_connection.return_value.__enter__.return_value = mock_conn
        mock_connection.return_value.__exit__.return_value = None

        prune = Prune(keep=5)
        prune()

        # Should show info message about nothing to prune
        mock_output.info.assert_called_with(
            "Only 3 versions found. Nothing to prune (keep=5)."
        )


# ============================================================================
# File Filtering
# ============================================================================


def test_prune_ignores_non_bundle_files(minimal_config_dict):
    """prune ignores files that don't match the bundle pattern."""
    config = msgspec.convert(minimal_config_dict, type=Config)
    mock_conn = MagicMock()

    # Mix of valid bundles and other files
    versions_output = (
        "testapp-1.2.0.pyz\n"
        "testapp-1.1.0.pyz\n"
        "testapp-1.0.0.pyz\n"
        "README.md\n"
        "backup.tar.gz\n"
        "other-app-1.0.0.pyz"  # Different app name
    )

    mock_conn.run.side_effect = [
        ("", True),  # test -d
        (versions_output, True),  # ls -1t
        ("", True),  # rm command
    ]

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch.object(Prune, "connection") as mock_connection,
        patch("fujin.commands.prune.Confirm") as mock_confirm,
        patch.object(Prune, "output", MagicMock()),
    ):
        mock_connection.return_value.__enter__.return_value = mock_conn
        mock_connection.return_value.__exit__.return_value = None
        mock_confirm.ask.return_value = True

        prune = Prune(keep=2)
        prune()

        # Verify only valid bundles are deleted
        calls = [call[0][0] for call in mock_conn.run.call_args_list]
        rm_cmd = [cmd for cmd in calls if "rm -f" in cmd][0]

        # Should delete oldest valid bundle
        assert "testapp-1.0.0.pyz" in rm_cmd

        # Should NOT delete non-bundle files or other app bundles
        assert "README.md" not in rm_cmd
        assert "backup.tar.gz" not in rm_cmd
        assert "other-app-1.0.0.pyz" not in rm_cmd
