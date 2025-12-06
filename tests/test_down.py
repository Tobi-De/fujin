from unittest.mock import patch, call
from fujin.commands.down import Down


from unittest.mock import patch
from fujin.commands.down import Down
from inline_snapshot import snapshot


def test_down_aborts_if_not_confirmed(mock_connection, get_commands):
    with patch("rich.prompt.Confirm.ask", return_value=False):
        down = Down()
        down()
        assert get_commands(mock_connection.mock_calls) == snapshot([])


def test_down_removes_files_and_stops_services(mock_connection, get_commands):
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
echo "Removing application directory..."
rm -rf "$APP_DIR"\
"""
            ]
        )


def test_down_full_uninstall_proxy(mock_connection, get_commands):
    with patch("rich.prompt.Confirm.ask", return_value=True):
        down = Down(full=True)
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
echo "Removing application directory..."
rm -rf "$APP_DIR"
sudo systemctl stop caddy
sudo systemctl disable caddy
sudo rm -f /usr/bin/caddy
sudo rm -f /etc/systemd/system/caddy.service
sudo userdel caddy
sudo rm -rf /etc/caddy\
"""
            ]
        )
