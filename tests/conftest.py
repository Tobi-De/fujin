import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch
from contextlib import contextmanager
from fujin.config import Config, HostConfig, Webserver, ProcessConfig, InstallationMode


@pytest.fixture
def capture_bundle(tmp_path):
    @contextmanager
    def _mock_temp_dir():
        yield str(tmp_path)

    with patch("tempfile.TemporaryDirectory", side_effect=_mock_temp_dir):
        yield tmp_path


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
    conn = MagicMock()
    conn.run.return_value = ("", True)
    return conn


@pytest.fixture(autouse=True)
def patch_host_connection(mock_connection):
    with patch("fujin.commands.BaseCommand.connection") as mock_ctx:
        mock_ctx.return_value.__enter__.return_value = mock_connection
        yield


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
            # Filter out non-run calls if using mock_connection.mock_calls
            if c[0] and c[0] != "run":
                continue

            if c.args:
                cmd = str(c.args[0])
            elif "command" in c.kwargs:
                cmd = str(c.kwargs["command"])
            else:
                continue

            env_prefix = "/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH"
            full_command = f'export PATH="{env_prefix}" && {cmd}'
            commands.append(full_command)
        return commands

    return _get


@pytest.fixture(autouse=True)
def silence_command_output():
    with patch("fujin.commands.BaseCommand.stdout"):
        yield
