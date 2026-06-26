"""Tests for prune command."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import msgspec
import pytest

from fujin.commands.prune import Prune
from fujin.config import Config


@pytest.mark.parametrize(
    "run_side_effects,keep,expected_message",
    [
        # No releases directory
        (
            [("", False)],
            2,
            "No releases directory found. Nothing to prune.",
        ),
        # Empty releases directory
        (
            [("", True), ("", True)],
            2,
            "No releases found to prune",
        ),
        # Fewer releases than keep
        (
            [("", True), ("1.0.0\n0.9.0", True)],
            3,
            "Only 2 release(s) found. Nothing to prune (keep=3).",
        ),
    ],
)
def test_prune_no_releases_scenarios(
    minimal_config_dict, run_side_effects, keep, expected_message
):
    """prune handles various no-releases scenarios correctly."""
    config = msgspec.convert(minimal_config_dict, type=Config)
    mock_conn = MagicMock()
    mock_conn.run.side_effect = run_side_effects

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch("fujin.connection.connection") as mock_connection,
        patch.object(Prune, "output", MagicMock()) as mock_output,
    ):
        mock_connection.return_value.__enter__.return_value = mock_conn
        mock_connection.return_value.__exit__.return_value = None

        prune = Prune(keep=keep)
        prune()

        mock_output.info.assert_called_with(expected_message)


def test_prune_deletes_old_releases_when_user_confirms(minimal_config_dict):
    """prune deletes old releases when user confirms."""
    config = msgspec.convert(minimal_config_dict, type=Config)
    mock_conn = MagicMock()

    # 4 releases, newest first
    mock_conn.run.side_effect = [
        ("", True),  # test -d releases/
        ("1.3.0\n1.2.0\n1.1.0\n1.0.0", True),  # ls -1t
        ("", True),  # rm -rf 1.1.0
        ("", True),  # rm -rf 1.0.0
    ]

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch("fujin.connection.connection") as mock_connection,
        patch("fujin.commands.prune.Confirm") as mock_confirm,
        patch.object(Prune, "output", MagicMock()) as mock_output,
    ):
        mock_connection.return_value.__enter__.return_value = mock_conn
        mock_connection.return_value.__exit__.return_value = None
        mock_confirm.ask.return_value = True

        prune = Prune(keep=2)
        prune()

        calls = [call[0][0] for call in mock_conn.run.call_args_list]
        # Should delete 1.1.0 and 1.0.0, keep 1.3.0 and 1.2.0
        assert any("rm -rf" in c and "1.1.0" in c for c in calls)
        assert any("rm -rf" in c and "1.0.0" in c for c in calls)
        assert not any("1.3.0" in c for c in calls if "rm -rf" in c)
        assert not any("1.2.0" in c for c in calls if "rm -rf" in c)

        mock_output.success.assert_called()


def test_prune_aborts_when_user_declines(minimal_config_dict):
    """prune aborts when user declines confirmation."""
    config = msgspec.convert(minimal_config_dict, type=Config)
    mock_conn = MagicMock()

    mock_conn.run.side_effect = [
        ("", True),  # test -d
        ("1.3.0\n1.2.0\n1.1.0\n1.0.0", True),  # ls -1t
    ]

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch("fujin.connection.connection") as mock_connection,
        patch("fujin.commands.prune.Confirm") as mock_confirm,
        patch.object(Prune, "output", MagicMock()),
    ):
        mock_connection.return_value.__enter__.return_value = mock_conn
        mock_connection.return_value.__exit__.return_value = None
        mock_confirm.ask.return_value = False

        prune = Prune(keep=2)
        prune()

        # Should NOT call rm
        calls = [call[0][0] for call in mock_conn.run.call_args_list]
        assert not any("rm -rf" in c for c in calls)


def test_prune_with_keep_1_deletes_all_but_newest(minimal_config_dict):
    """prune with --keep 1 deletes all but the newest release."""
    config = msgspec.convert(minimal_config_dict, type=Config)
    mock_conn = MagicMock()

    mock_conn.run.side_effect = [
        ("", True),  # test -d
        ("2.0.0\n1.0.0\n0.9.0", True),  # ls -1t
        ("", True),  # rm -rf 1.0.0
        ("", True),  # rm -rf 0.9.0
    ]

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch("fujin.connection.connection") as mock_connection,
        patch("fujin.commands.prune.Confirm") as mock_confirm,
        patch.object(Prune, "output", MagicMock()),
    ):
        mock_connection.return_value.__enter__.return_value = mock_conn
        mock_connection.return_value.__exit__.return_value = None
        mock_confirm.ask.return_value = True

        prune = Prune(keep=1)
        prune()

        calls = [call[0][0] for call in mock_conn.run.call_args_list]
        assert any("rm -rf" in c and "1.0.0" in c for c in calls)
        assert any("rm -rf" in c and "0.9.0" in c for c in calls)
        assert not any("2.0.0" in c for c in calls if "rm -rf" in c)


def test_prune_with_no_filtering_needed(minimal_config_dict):
    """prune with keep >= count does nothing."""
    config = msgspec.convert(minimal_config_dict, type=Config)
    mock_conn = MagicMock()

    mock_conn.run.side_effect = [
        ("", True),  # test -d
        ("1.0.0\n0.9.0", True),  # ls -1t
    ]

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch("fujin.connection.connection") as mock_connection,
        patch.object(Prune, "output", MagicMock()) as mock_output,
    ):
        mock_connection.return_value.__enter__.return_value = mock_conn
        mock_connection.return_value.__exit__.return_value = None

        prune = Prune(keep=5)
        prune()

        mock_output.info.assert_called_with(
            "Only 2 release(s) found. Nothing to prune (keep=5)."
        )


def test_prune_keep_zero_raises_error(minimal_config_dict):
    """prune with --keep 0 raises error."""
    config = msgspec.convert(minimal_config_dict, type=Config)

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch.object(Prune, "output", MagicMock()),
    ):
        with pytest.raises(SystemExit) as exc:
            prune = Prune(keep=0)
            prune()
        assert exc.value.code == 1
