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
                "rm -rf /home/testuser/.local/share/fujin/testapp",
                "sudo rm /etc/caddy/conf.d/testapp.caddy",
                "sudo systemctl reload caddy",
                "sudo systemctl disable --now testapp.service testapp-worker@1.service testapp-worker@2.service",
                "sudo rm /etc/systemd/system/testapp.service /etc/systemd/system/testapp-worker@.service",
                "sudo systemctl daemon-reload",
                "sudo systemctl reset-failed",
            ]
        )


def test_down_full_uninstall_proxy(mock_connection, get_commands):
    with patch("rich.prompt.Confirm.ask", return_value=True):
        down = Down(full=True)
        down()

        assert get_commands(mock_connection.mock_calls) == snapshot(
            [
                "rm -rf /home/testuser/.local/share/fujin/testapp",
                "sudo rm /etc/caddy/conf.d/testapp.caddy",
                "sudo systemctl reload caddy",
                "sudo systemctl disable --now testapp.service testapp-worker@1.service testapp-worker@2.service",
                "sudo rm /etc/systemd/system/testapp.service /etc/systemd/system/testapp-worker@.service",
                "sudo systemctl daemon-reload",
                "sudo systemctl reset-failed",
                "sudo systemctl stop caddy",
                "sudo systemctl disable caddy",
                "sudo rm /usr/bin/caddy",
                "sudo rm /etc/systemd/system/caddy.service",
                "sudo userdel caddy",
                "sudo rm -rf /etc/caddy",
            ]
        )
