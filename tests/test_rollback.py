from unittest.mock import patch, MagicMock
from fujin.commands.rollback import Rollback
from inline_snapshot import snapshot


def test_rollback(mock_connection, get_commands):
    def run_side_effect(command, **kwargs):
        stdout = ""
        if "ls -1t" in command:
            stdout = "testapp-0.1.0.tar.gz\ntestapp-0.0.9.tar.gz\ntestapp-0.0.8.tar.gz"
        elif "cat" in command and ".version" in command:
            stdout = "0.1.0"
        elif "test -f" in command:
            return "", True
        return stdout, True

    mock_connection.run.side_effect = run_side_effect

    with (
        patch("rich.prompt.Prompt.ask", return_value="0.0.9"),
        patch("rich.prompt.Confirm.ask", return_value=True),
    ):
        rollback = Rollback()
        rollback()

        assert get_commands(mock_connection.mock_calls) == snapshot(
            [
                'export PATH="/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH" && ls -1t /home/testuser/.local/share/fujin/testapp/.versions',
                'export PATH="/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH" && cat /home/testuser/.local/share/fujin/testapp/.version',
                'export PATH="/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH" && test -f /home/testuser/.local/share/fujin/testapp/.versions/testapp-0.1.0.tar.gz',
                'export PATH="/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH" && mkdir -p /tmp/uninstall-testapp-0.1.0 && tar -xzf /home/testuser/.local/share/fujin/testapp/.versions/testapp-0.1.0.tar.gz -C /tmp/uninstall-testapp-0.1.0',
                'export PATH="/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH" && test -f /tmp/uninstall-testapp-0.1.0/uninstall.sh',
                'export PATH="/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH" && cd /tmp/uninstall-testapp-0.1.0 && chmod +x ./uninstall.sh && bash ./uninstall.sh',
                'export PATH="/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH" && rm -rf /tmp/uninstall-testapp-0.1.0',
                'export PATH="/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH" && mkdir -p /tmp/testapp-0.0.9 && tar -xzf /home/testuser/.local/share/fujin/testapp/.versions/testapp-0.0.9.tar.gz -C /tmp/testapp-0.0.9 && cd /tmp/testapp-0.0.9 && bash install.sh && cd / && rm -rf /tmp/testapp-0.0.9',
            ]
        )
