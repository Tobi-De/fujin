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
    """Records remote commands and answers based on their content.

    ``bundle_exists`` answers the legacy ``.pyz`` probe, ``version_exists``
    the ``.version`` read, and any command containing a string in
    ``fail_on`` reports failure.
    """

    def __init__(
        self,
        *,
        bundle_exists: bool = False,
        version_exists: bool = True,
        version_value: str = "2.3.4",
        fail_on: tuple[str, ...] = (),
    ):
        self.runs: list[str] = []
        self.bundle_exists = bundle_exists
        self.version_exists = version_exists
        self.version_value = version_value
        self.fail_on = fail_on

    def run(self, cmd, **kw):
        self.runs.append(cmd)
        if "cat " in cmd and ".version" in cmd:
            return (self.version_value if self.version_exists else ""), (
                self.version_exists
            )
        if "test -f " in cmd and ".pyz" in cmd:
            return "", self.bundle_exists
        if any(marker in cmd for marker in self.fail_on):
            return "", False
        return "", True


# ============================================================================
# Legacy .pyz layout: warn, never run an interpreter as root, keep tearing down
# ============================================================================


def test_down_legacy_bundle_warns_and_continues(minimal_config_dict):
    """A legacy .pyz host is warned about and then torn down anyway."""
    config = msgspec.convert(minimal_config_dict, type=Config)
    conn = _RecordingConn(bundle_exists=True)

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch("fujin.connection.connection") as mock_connection,
        patch("fujin.commands.down.Confirm") as mock_confirm,
        patch("fujin.commands.down.log_operation") as mock_log,
        patch.object(Down, "output", MagicMock()) as output,
    ):
        mock_connection.return_value.__enter__.return_value = conn
        mock_connection.return_value.__exit__.return_value = None
        mock_confirm.ask.return_value = True

        Down()()

    warnings = [call.args[0] for call in output.warning.call_args_list]
    joined_warnings = "\n".join(warnings)
    assert ".pyz" in joined_warnings
    assert "administrator" in joined_warnings
    assert "python3" in joined_warnings
    assert "uninstall" in joined_warnings

    # No root interpreter invocation anywhere.
    joined = "\n".join(conn.runs)
    assert "python3" not in joined

    # The rest of the teardown still runs.
    assert any("sudo rm -rf /etc/systemd/system/" in cmd for cmd in conn.runs)
    assert any("sudo systemctl daemon-reload" in cmd for cmd in conn.runs)
    assert any(f"sudo rm -rf {config.app_dir}" in cmd for cmd in conn.runs)
    assert any(f"userdel {config.app_user}" in cmd for cmd in conn.runs)
    mock_log.assert_called_once()
    output.success.assert_called_once()


def test_down_uses_config_version_when_version_file_missing(minimal_config_dict):
    """Down falls back to the config version for the legacy bundle probe."""
    config = msgspec.convert(minimal_config_dict, type=Config)
    conn = _RecordingConn(bundle_exists=True, version_exists=False)

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

        Down()()

    probes = [cmd for cmd in conn.runs if "test -f " in cmd and ".pyz" in cmd]
    assert probes
    assert f"{config.app_name}-{config.version}.pyz" in probes[0]
    assert mock_log.call_args.kwargs["version"] == config.version


def test_down_reads_version_from_version_file(minimal_config_dict):
    """The .version file on the host wins over the config version."""
    config = msgspec.convert(minimal_config_dict, type=Config)
    conn = _RecordingConn(bundle_exists=True, version_value="9.8.7")

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

        Down()()

    probes = [cmd for cmd in conn.runs if "test -f " in cmd and ".pyz" in cmd]
    assert probes
    assert "testapp-9.8.7.pyz" in probes[0]
    assert mock_log.call_args.kwargs["version"] == "9.8.7"


# ============================================================================
# Force Flag Behavior
# ============================================================================


def test_down_fails_when_teardown_fails_without_force(minimal_config_dict):
    """A failing teardown step aborts with exit code 1 unless --force is set."""
    config = msgspec.convert(minimal_config_dict, type=Config)
    conn = _RecordingConn(bundle_exists=True, fail_on=("systemctl daemon-reload",))

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch("fujin.connection.connection") as mock_connection,
        patch("fujin.commands.down.Confirm") as mock_confirm,
        patch("fujin.commands.down.log_operation") as mock_log,
        patch.object(Down, "output", MagicMock()) as output,
    ):
        mock_connection.return_value.__enter__.return_value = conn
        mock_connection.return_value.__exit__.return_value = None
        mock_confirm.ask.return_value = True

        with pytest.raises(SystemExit) as exc_info:
            Down(force=False)()

    assert exc_info.value.code == 1
    # The legacy warning is still emitted before the failure.
    assert any(".pyz" in call.args[0] for call in output.warning.call_args_list), (
        output.warning.call_args_list
    )
    assert "python3" not in "\n".join(conn.runs)
    mock_log.assert_not_called()


def test_down_continues_with_force_when_teardown_fails(minimal_config_dict):
    """--force continues the teardown (incl. Caddy) after a failed step."""
    config = msgspec.convert(minimal_config_dict, type=Config)
    conn = _RecordingConn(bundle_exists=True, fail_on=("systemctl daemon-reload",))
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
    # --full still reaches Caddy teardown on a legacy host.
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
    conn = _RecordingConn(bundle_exists=True)
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

        Down(full=True)()

    # Legacy host: warned, but Caddy is still uninstalled.
    assert any(".pyz" in call.args[0] for call in output.warning.call_args_list)
    mock_caddy.get_uninstall_commands.assert_called_once()
    assert any(
        "systemctl stop caddy" in cmd and "apt remove" in cmd for cmd in conn.runs
    )
    assert mock_log.call_args.kwargs["operation"] == "full-down"


# ============================================================================
# Release-tree teardown: Caddy config + app user (parity with old installer)
# ============================================================================


def _release_tree_config(minimal_config_dict):
    config = msgspec.convert(minimal_config_dict, type=Config)
    # force the release-tree branch: no legacy bundle on the server
    return config


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
