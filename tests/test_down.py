from unittest.mock import patch
from fujin.commands.down import Down
from inline_snapshot import snapshot
from tests.script_runner import script_runner  # noqa: F401
import tarfile
import io


def test_down_aborts_if_not_confirmed(mock_connection, get_commands):
    with patch("rich.prompt.Confirm.ask", return_value=False):
        down = Down()
        down()
        assert get_commands(mock_connection.mock_calls) == snapshot([])


def test_down_command_generation(mock_connection, get_commands):
    with patch("rich.prompt.Confirm.ask", return_value=True):
        down = Down()
        down()

        assert get_commands(mock_connection.mock_calls) == snapshot(
            [
                """\
export PATH="/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH" && #!/usr/bin/env bash
set -e
APP_DIR=/home/testuser/.local/share/fujin/testapp
APP_NAME=testapp
if [ -f "$APP_DIR/.version" ]; then
  CURRENT_VERSION=$(cat "$APP_DIR/.version")
  CURRENT_BUNDLE="$APP_DIR/.versions/$APP_NAME-$CURRENT_VERSION.tar.gz"
  if [ -f "$CURRENT_BUNDLE" ]; then
    TMP_DIR="/tmp/uninstall-$APP_NAME-$CURRENT_VERSION"
    mkdir -p "$TMP_DIR"
    if tar -xzf "$CURRENT_BUNDLE" -C "$TMP_DIR"; then
      if [ -f "$TMP_DIR/uninstall.sh" ]; then
        echo "Running uninstall script for version $CURRENT_VERSION..."
        chmod +x "$TMP_DIR/uninstall.sh"
        bash "$TMP_DIR/uninstall.sh"
      else
        echo "Warning: uninstall.sh not found in bundle."
        if [ -z "$FORCE" ]; then exit 1; fi
      fi
    else
      echo "Warning: Failed to extract bundle."
      if [ -z "$FORCE" ]; then exit 1; fi
    fi
    rm -rf "$TMP_DIR"
  fi
fi
sudo systemctl disable --now testapp.service testapp-worker@1.service testapp-worker@2.service 2>/dev/null || true
echo "Removing application directory..."
rm -rf "$APP_DIR"\
"""
            ]
        )


def test_down_script_execution(mock_connection, script_runner, mock_config):
    # Setup environment
    app_dir = script_runner.root / "home/testuser/.local/share/fujin/testapp"
    app_dir.mkdir(parents=True)
    (app_dir / ".version").write_text("0.1.0")

    versions_dir = app_dir / ".versions"
    versions_dir.mkdir()

    # Create a fake bundle with uninstall script
    bundle_path = versions_dir / "testapp-0.1.0.tar.gz"

    uninstall_script = """#!/bin/bash
echo "Uninstalling..."
# Create a marker to prove we ran
touch uninstall_ran.marker
"""

    # Create tarball in memory
    bio = io.BytesIO()
    with tarfile.open(fileobj=bio, mode="w:gz") as tar:
        info = tarfile.TarInfo("uninstall.sh")
        info.size = len(uninstall_script)
        tar.addfile(info, io.BytesIO(uninstall_script.encode()))

    bundle_path.write_bytes(bio.getvalue())

    # Mock connection to capture the command
    captured_command = []

    def run_side_effect(cmd, **kwargs):
        captured_command.append(cmd)
        return "", True

    mock_connection.run.side_effect = run_side_effect

    # Mock system commands that might be dangerous or fail
    script_runner._create_mock("userdel", "echo userdel $@")

    # Run with full=True to test service stopping as well
    with patch("rich.prompt.Confirm.ask", return_value=True):
        down = Down(full=True)
        down()

    # Extract the script from the command
    script_content = captured_command[0]

    result = script_runner.run(script_content)
    result.assert_success()

    # Verify app dir is removed
    assert not app_dir.exists()

    # Verify uninstall script ran
    assert (script_runner.root / "uninstall_ran.marker").exists()

    # Verify system services were stopped (full=True behavior)
    systemctl_log = result.get_log("systemctl")
    assert "stop caddy" in systemctl_log
    assert "disable caddy" in systemctl_log
