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
# Fake connection
# ============================================================================


class _RecordingConn:
    """Records remote commands; a command containing a string in ``fail_on``
    reports failure."""

    def __init__(self, *, fail_on: tuple[str, ...] = ()):
        self.runs: list[str] = []
        self.fail_on = fail_on

    def run(self, cmd, **kw):
        self.runs.append(cmd)
        if any(marker in cmd for marker in self.fail_on):
            return "", False
        return "", True


# ============================================================================
# Force Flag Behavior
# ============================================================================


def test_down_fails_when_teardown_fails_without_force(minimal_config_dict):
    """A failing teardown step aborts with exit code 1 unless --force is set."""
    config = msgspec.convert(minimal_config_dict, type=Config)
    conn = _RecordingConn(fail_on=("systemctl daemon-reload",))

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch("fujin.connection.connection") as mock_connection,
        patch("fujin.commands.down.Confirm") as mock_confirm,
        patch("fujin.commands.down.log_operation") as mock_log,
        patch.object(Down, "output", MagicMock()),
    ):
        mock_connection.return_value.__enter__.return_value = conn
        mock_connection.return_value.__exit__.return_value = None
        mock_confirm.ask.return_value = True

        with pytest.raises(SystemExit) as exc_info:
            Down(force=False)()

    assert exc_info.value.code == 1
    mock_log.assert_not_called()


def test_down_continues_with_force_when_teardown_fails(minimal_config_dict):
    """--force continues the teardown (incl. Caddy) after a failed step."""
    config = msgspec.convert(minimal_config_dict, type=Config)
    conn = _RecordingConn(fail_on=("systemctl daemon-reload",))
    mock_caddy = MagicMock()
    mock_caddy.get_uninstall_commands.return_value = [
        "systemctl stop caddy",
        "apt remove -y caddy",
    ]

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch("fujin.connection.connection") as mock_connection,
        patch("fujin.commands.down.Confirm") as mock_confirm,
        patch("fujin.commands.down.log_operation") as mock_log,
        patch("fujin.commands.down.caddy", mock_caddy),
        patch.object(Down, "output", MagicMock()) as output,
    ):
        mock_connection.return_value.__enter__.return_value = conn
        mock_connection.return_value.__exit__.return_value = None
        mock_confirm.ask.return_value = True

        Down(force=True, full=True)()

    assert any("--force" in call.args[0] for call in output.warning.call_args_list), (
        output.warning.call_args_list
    )

    # Force cleanup re-removes the app dir after the failed combined step.
    assert f"sudo rm -rf {config.app_dir}" in conn.runs
    # --full still reaches Caddy teardown after a failed step.
    mock_caddy.get_uninstall_commands.assert_called_once()
    assert any(
        "systemctl stop caddy" in cmd and "apt remove" in cmd for cmd in conn.runs
    )
    mock_log.assert_called_once()


# ============================================================================
# Full Flag Behavior
# ============================================================================


def test_down_with_full_flag_uninstalls_caddy(minimal_config_dict):
    """Down with --full flag also uninstalls Caddy."""
    config = msgspec.convert(minimal_config_dict, type=Config)
    conn = _RecordingConn()
    mock_caddy = MagicMock()
    mock_caddy.get_uninstall_commands.return_value = [
        "systemctl stop caddy",
        "apt remove -y caddy",
    ]

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch("fujin.connection.connection") as mock_connection,
        patch("fujin.commands.down.Confirm") as mock_confirm,
        patch("fujin.commands.down.log_operation") as mock_log,
        patch("fujin.commands.down.caddy", mock_caddy),
        patch.object(Down, "output", MagicMock()),
    ):
        mock_connection.return_value.__enter__.return_value = conn
        mock_connection.return_value.__exit__.return_value = None
        mock_confirm.ask.return_value = True

        Down(full=True)()

    mock_caddy.get_uninstall_commands.assert_called_once()
    assert any(
        "systemctl stop caddy" in cmd and "apt remove" in cmd for cmd in conn.runs
    )
    assert mock_log.call_args.kwargs["operation"] == "full-down"


# ============================================================================
# Release-tree teardown: Caddy config + app user (parity with old installer)
# ============================================================================


def _release_tree_config(minimal_config_dict):
    return msgspec.convert(minimal_config_dict, type=Config)


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
