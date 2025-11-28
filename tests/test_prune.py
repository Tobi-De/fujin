from unittest.mock import patch
from fujin.commands.prune import Prune
from inline_snapshot import snapshot


def test_prune(mock_connection, get_commands):
    mock_connection.run.return_value.stdout = "0.0.8\n0.0.7"

    with patch("rich.prompt.Confirm.ask", return_value=True):
        prune = Prune(keep=2)
        prune()

        assert get_commands(mock_connection.mock_calls) == snapshot(
            [
                "sed -n '3,$p' .versions",
                "rm -r /home/testuser/.local/share/fujin/testapp/v0.0.8 /home/testuser/.local/share/fujin/testapp/v0.0.7",
                "sed -i '3,$d' .versions",
            ]
        )
