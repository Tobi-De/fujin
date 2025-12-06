from unittest.mock import patch
from fujin.commands.prune import Prune
from inline_snapshot import snapshot


def test_prune(mock_connection, get_commands):
    def run_side_effect(command, **kwargs):
        if "test -d" in command:
            return ("", True)
        if "ls -1t" in command:
            return (
                "testapp-0.0.4.tar.gz\ntestapp-0.0.3.tar.gz\ntestapp-0.0.2.tar.gz\ntestapp-0.0.1.tar.gz",
                True,
            )
        return ("", True)

    mock_connection.run.side_effect = run_side_effect

    with patch("rich.prompt.Confirm.ask", return_value=True):
        prune = Prune(keep=2)
        prune()

        assert get_commands(mock_connection.mock_calls) == snapshot(
            [
                'export PATH="/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH" && test -d /home/testuser/.local/share/fujin/testapp/.versions',
                'export PATH="/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH" && ls -1t /home/testuser/.local/share/fujin/testapp/.versions',
                'export PATH="/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH" && cd /home/testuser/.local/share/fujin/testapp/.versions && rm -f testapp-0.0.2.tar.gz testapp-0.0.1.tar.gz',
            ]
        )
