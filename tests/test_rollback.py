from unittest.mock import patch, MagicMock
from fujin.commands.rollback import Rollback
from inline_snapshot import snapshot


def test_rollback(mock_connection, get_commands):
    def run_side_effect(command, **kwargs):
        stdout = ""
        if "sed -n '2,$p' .versions" in command:
            stdout = "0.0.9\n0.0.8"
        elif "head -n 1 .versions" in command:
            stdout = "0.1.0"
        return stdout, True

    mock_connection.run.side_effect = run_side_effect

    with (
        patch("rich.prompt.Prompt.ask", return_value="0.0.9"),
        patch("rich.prompt.Confirm.ask", return_value=True),
    ):
        rollback = Rollback()
        rollback()

        assert get_commands(mock_connection.mock_calls) == snapshot(
            ["ls -1t /home/testuser/.local/share/fujin/testapp/.versions"]
        )
