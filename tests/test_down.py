"""Tests for down command - user interaction and error handling.

Uses shared minimal_config_dict fixture from conftest.py.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import msgspec
import pytest

from fujin.commands.down import Down
from fujin.config import Config

# ============================================================================
# Confirmation Flow
# ============================================================================


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
        mock_confirm.ask.return_value = False  # User declines

        down = Down()
        down()

        # Connection should not be used if user declines
        assert not mock_conn.run.called


def test_down_handles_keyboard_interrupt(minimal_config_dict):
    """Down command handles Ctrl+C gracefully during confirmation."""
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


# ============================================================================
# Successful Teardown
# ============================================================================


def test_down_successful_teardown_with_bundle(minimal_config_dict):
    """Down successfully tears down when bundle exists."""
    config = msgspec.convert(minimal_config_dict, type=Config)
    mock_conn = MagicMock()

    # Mock responses for: version read, bundle exists check, uninstall
    mock_conn.run.side_effect = [
        ("1.0.0", True),  # cat .version
        ("", True),  # test -f bundle (exists)
        ("", True),  # uninstall command
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

        # Verify uninstall command was called
        calls = [call[0][0] for call in mock_conn.run.call_args_list]
        assert any("python3" in cmd and "uninstall" in cmd for cmd in calls)
        assert any("rm -rf" in cmd for cmd in calls)


def test_down_uses_config_version_when_version_file_missing(minimal_config_dict):
    """Down uses config version when .version file doesn't exist."""
    config = msgspec.convert(minimal_config_dict, type=Config)
    mock_conn = MagicMock()

    # Version file read fails, bundle check, uninstall
    mock_conn.run.side_effect = [
        ("", False),  # cat .version fails
        ("", True),  # test -f bundle (exists)
        ("", True),  # uninstall command
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

        # Should have tried to run uninstall with config version
        calls = [call[0][0] for call in mock_conn.run.call_args_list]
        assert any("testapp-1.0.0.pyz" in cmd for cmd in calls)


# ============================================================================
# Force Flag Behavior
# ============================================================================


def test_down_fails_when_uninstall_fails_without_force(minimal_config_dict):
    """Down raises error when uninstall fails and --force not set."""
    config = msgspec.convert(minimal_config_dict, type=Config)
    mock_conn = MagicMock()

    # Version read, bundle exists, uninstall fails
    mock_conn.run.side_effect = [
        ("1.0.0", True),  # cat .version
        ("", True),  # test -f bundle (exists)
        ("", False),  # uninstall command fails
    ]

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch("fujin.connection.connection") as mock_connection,
        patch("fujin.commands.down.Confirm") as mock_confirm,
        patch.object(Down, "output", MagicMock()),
    ):
        mock_connection.return_value.__enter__.return_value = mock_conn
        mock_connection.return_value.__exit__.return_value = None
        mock_confirm.ask.return_value = True

        down = Down(force=False)

        with pytest.raises(SystemExit) as exc_info:
            down()

        assert exc_info.value.code == 1


def test_down_continues_with_force_when_uninstall_fails(minimal_config_dict):
    """Down continues with force cleanup when uninstall fails and --force set."""
    config = msgspec.convert(minimal_config_dict, type=Config)
    mock_conn = MagicMock()

    # Version read, bundle exists, uninstall fails, force cleanup
    mock_conn.run.side_effect = [
        ("1.0.0", True),  # cat .version
        ("", True),  # test -f bundle (exists)
        ("", False),  # uninstall command fails
        ("", True),  # rm -rf (force cleanup)
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

        down = Down(force=True)
        down()

        # Verify force cleanup was called
        calls = [call[0][0] for call in mock_conn.run.call_args_list]
        # Should have two rm -rf calls: one in uninstall, one for force cleanup
        assert sum("rm -rf" in cmd for cmd in calls) == 2


# ============================================================================
# Full Flag Behavior
# ============================================================================


def test_down_with_full_flag_uninstalls_caddy(minimal_config_dict):
    """Down with --full flag also uninstalls Caddy."""
    config = msgspec.convert(minimal_config_dict, type=Config)
    mock_conn = MagicMock()

    # Version read, bundle exists, uninstall, caddy uninstall
    mock_conn.run.side_effect = [
        ("1.0.0", True),  # cat .version
        ("", True),  # test -f bundle (exists)
        ("", True),  # uninstall command
        ("", True),  # caddy uninstall commands
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

        # Verify Caddy uninstall was attempted
        mock_caddy.get_uninstall_commands.assert_called_once()
        calls = [call[0][0] for call in mock_conn.run.call_args_list]
        assert any(
            "systemctl stop caddy" in cmd and "apt remove" in cmd for cmd in calls
        )


# ============================================================================
# Release-tree teardown: Caddy config + app user (parity with old installer)
# ============================================================================


def _release_tree_config(minimal_config_dict):
    import msgspec

    config = msgspec.convert(minimal_config_dict, type=Config)
    # force the release-tree branch: no legacy bundle on the server
    return config


class _RecordingConn:
    def __init__(self):
        self.runs: list[str] = []

    def run(self, cmd, **kw):
        self.runs.append(cmd)
        # There is no legacy bundle in the release-tree branch.
        if "test -f " in cmd and ".pyz" in cmd:
            return "", False
        return "", True


def test_down_release_tree_removes_caddy_config_and_app_user(
    minimal_config_dict, monkeypatch
):
    """The release-tree branch unlinks the per-app Caddy config, reloads
    caddy, and deletes the app user (old-installer parity)."""
    config = _release_tree_config(minimal_config_dict)
    fake_conn = _RecordingConn()
    with (
        patch("fujin.config.Config.read", return_value=config),
        patch("fujin.connection.connection") as mock_connection,
        patch("fujin.commands.down.Confirm") as mock_confirm,
        patch("fujin.commands.down.log_operation"),
        patch.object(Down, "output", MagicMock()),
    ):
        mock_connection.return_value.__enter__.return_value = fake_conn
        mock_connection.return_value.__exit__.return_value = None
        mock_confirm.ask.return_value = True

        down = Down()
        down.config = config
        monkeypatch.setattr(
            type(config), "caddyfile_exists", property(lambda self: True)
        )

        down()

    calls = fake_conn.runs
    assert any(f"/etc/caddy/conf.d/{config.app_name}.caddy" in cmd for cmd in calls), (
        calls
    )
    assert any("systemctl reload caddy" in cmd for cmd in calls)
    joined = "\n".join(calls)
    assert f"userdel {config.app_user}" in joined
    assert f"pkill -u {config.app_user}" in joined


def test_down_release_tree_survives_missing_app_user(minimal_config_dict, monkeypatch):
    """`userdel` on an already-absent app user is non-fatal (|| true)."""
    config = _release_tree_config(minimal_config_dict)
    fake_conn = _RecordingConn()
    with (
        patch("fujin.config.Config.read", return_value=config),
        patch("fujin.connection.connection") as mock_connection,
        patch("fujin.commands.down.Confirm") as mock_confirm,
        patch("fujin.commands.down.log_operation"),
        patch.object(Down, "output", MagicMock()),
    ):
        mock_connection.return_value.__enter__.return_value = fake_conn
        mock_connection.return_value.__exit__.return_value = None
        mock_confirm.ask.return_value = True

        down = Down()
        down.config = config
        monkeypatch.setattr(
            type(config), "caddyfile_exists", property(lambda self: False)
        )

        down()

    joined = "\n".join(fake_conn.runs)
    assert "userdel" in joined
    assert "|| true" in joined
