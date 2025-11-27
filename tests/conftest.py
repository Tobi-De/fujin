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
def mock_connection():
    with patch("fujin.commands._base.host_connection") as mock:
        conn = MagicMock()
        # Setup context manager behavior for the connection itself
        mock.return_value.__enter__.return_value = conn

        # Setup context manager behavior for conn.cd() and conn.prefix()
        # These methods return context managers that yield the connection (or None)
        conn.cd.return_value.__enter__.return_value = conn
        conn.prefix.return_value.__enter__.return_value = conn

        yield conn


@pytest.fixture
def mock_calls(mock_connection):
    return mock_connection.run.call_args_list


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
            name = c[0]
            if name == "run":
                if c.args:
                    commands.append(str(c.args[0]))
                elif "command" in c.kwargs:
                    commands.append(str(c.kwargs["command"]))
        return commands

    return _get
