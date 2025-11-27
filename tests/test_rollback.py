from unittest.mock import patch, MagicMock
from fujin.commands.rollback import Rollback
from inline_snapshot import snapshot


def test_rollback(mock_connection, get_commands):
    def run_side_effect(command, **kwargs):
        mock = MagicMock()
        if "sed -n '2,$p' .versions" in command:
            mock.stdout = "0.0.9\n0.0.8"
        elif "head -n 1 .versions" in command:
            mock.stdout = "0.1.0"
        else:
            mock.stdout = ""
        return mock

    mock_connection.run.side_effect = run_side_effect

    with (
        patch("rich.prompt.Prompt.ask", return_value="0.0.9"),
        patch("rich.prompt.Confirm.ask", return_value=True),
    ):
        rollback = Rollback()
        rollback()

        assert get_commands(mock_connection.mock_calls) == snapshot(
            [
                "sed -n '2,$p' .versions",
                "head -n 1 .versions",
                "mkdir -p /home/testuser/.local/share/fujin/testapp/v0.0.9",
                """\
echo 'set -a  # Automatically export all variables
source .env
set +a  # Stop automatic export
export UV_COMPILE_BYTECODE=1
export UV_PYTHON=python3.12
export PATH=".venv/bin:$PATH"' > /home/testuser/.local/share/fujin/testapp/.appenv\
""",
                "sudo rm -rf .venv",
                "uv python install 3.12",
                "uv venv",
                "uv pip install /home/testuser/.local/share/fujin/testapp/v0.0.9/testapp-0.0.9.whl",
                "head -n 1 .versions",
                "sed -i '1i 0.0.9' .versions",
                "sudo systemctl restart testapp.service testapp-worker@1.service testapp-worker@2.service",
                "rm -r v0.1.0",
                "sed -i '1,/0.0.9/{/0.0.9/!d}' .versions",
            ]
        )
