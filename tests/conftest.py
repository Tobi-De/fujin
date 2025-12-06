import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch
from fujin.config import Config, HostConfig, Webserver, ProcessConfig, InstallationMode


@pytest.fixture
def mock_config():
    return Config(
        app_name="testapp",
        version="0.1.0",
        build_command="echo build",
        distfile="dist/testapp-{version}.whl",
        installation_mode=InstallationMode.PY_PACKAGE,
        python_version="3.12",
        host=HostConfig(
            domain_name="example.com",
            user="testuser",
            env_content="FOO=bar",
        ),
        webserver=Webserver(upstream="localhost:8000"),
        processes={
            "web": ProcessConfig(command="run web"),
            "worker": ProcessConfig(command="run worker", replicas=2),
        },
        local_config_dir=Path(__file__).parent.parent / "src" / "fujin" / "templates",
    )


@pytest.fixture
def mock_ssh_channel():
    with (
        patch("fujin.connection.socket"),
        patch("fujin.connection.Session") as mock_session_cls,
        patch("fujin.connection.select") as mock_select,
    ):

        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        mock_session.userauth_authenticated.return_value = True

        mock_channel = MagicMock()
        mock_session.open_session.return_value = mock_channel

        # Default behavior
        mock_channel.eof.return_value = True
        mock_channel.read.return_value = (0, b"")
        mock_channel.read_stderr.return_value = (0, b"")
        mock_channel.get_exit_status.return_value = 0

        mock_select.return_value = ([], [], [])

        yield mock_channel


@pytest.fixture
def mock_connection(mock_ssh_channel):
    return mock_ssh_channel


@pytest.fixture
def mock_calls(mock_connection):
    return mock_connection.execute.call_args_list


@pytest.fixture(autouse=True)
def patch_config_read(mock_config):
    """Automatically patch Config.read for all tests."""
    with patch("fujin.config.Config.read", return_value=mock_config):
        yield


@pytest.fixture
def get_commands():
    def _get(mock_calls):
        commands = []
        for c in mock_calls:
            if c.args:
                commands.append(str(c.args[0]))
            elif "command" in c.kwargs:
                commands.append(str(c.kwargs["command"]))
        return commands

    return _get
