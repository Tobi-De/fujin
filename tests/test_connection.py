import socket
import pytest
import cappa
from unittest.mock import MagicMock, patch, call
from fujin.connection import SSH2Connection
from fujin.config import HostConfig


@pytest.fixture
def mock_ssh_components():
    """
    Mocks the low-level ssh2-python components.
    Returns a tuple of (mock_session, mock_channel, mock_socket).
    """
    with (
        patch("fujin.connection.Session") as mock_session_cls,
        patch("fujin.connection.select") as mock_select,
    ):
        mock_sock = MagicMock(spec=socket.socket)
        
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        mock_session.userauth_authenticated.return_value = True
        
        mock_channel = MagicMock()
        mock_session.open_session.return_value = mock_channel
        
        # Default channel behavior
        mock_channel.read.return_value = (0, b"")
        mock_channel.read_stderr.return_value = (0, b"")
        mock_channel.get_exit_status.return_value = 0
        
        # Default select behavior (ready to read)
        mock_select.return_value = ([], [], [])

        yield mock_session, mock_channel, mock_sock


@pytest.fixture
def connection(mock_ssh_components):
    mock_session, _, mock_sock = mock_ssh_components
    host = HostConfig(domain_name="example.com", user="testuser")
    return SSH2Connection(mock_session, host, mock_sock)


def test_run_command_structure(connection, mock_ssh_components):
    _, mock_channel, _ = mock_ssh_components
    
    connection.run("echo hello")
    
    # Verify the command structure (PATH export + command)
    expected_cmd = 'export PATH="/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH" && echo hello'
    mock_channel.execute.assert_called_with(expected_cmd)


def test_cd_context_manager(connection, mock_ssh_components):
    _, mock_channel, _ = mock_ssh_components
    
    with connection.cd("/var/www"):
        connection.run("ls")
        
        expected_cmd_1 = 'export PATH="/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH" && cd /var/www && ls'
        mock_channel.execute.assert_called_with(expected_cmd_1)
        
        with connection.cd("html"):
            connection.run("touch index.html")
            
            expected_cmd_2 = 'export PATH="/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH" && cd /var/www/html && touch index.html'
            mock_channel.execute.assert_called_with(expected_cmd_2)

    # Should be back to root (empty cwd)
    connection.run("whoami")
    expected_cmd_3 = 'export PATH="/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH" && whoami'
    mock_channel.execute.assert_called_with(expected_cmd_3)


def test_run_returns_stdout(connection, mock_ssh_components):
    _, mock_channel, _ = mock_ssh_components
    
    # Simulate output: "hello" then empty (EOF)
    mock_channel.read.side_effect = [(5, b"hello"), (0, b"")]
    
    stdout, success = connection.run("echo hello", hide=True)
    
    assert stdout == "hello"
    assert success is True


def test_run_returns_failure_on_exit_code(connection, mock_ssh_components):
    _, mock_channel, _ = mock_ssh_components
    
    mock_channel.get_exit_status.return_value = 1
    
    # Default behavior is to raise cappa.Exit on failure
    with pytest.raises(cappa.Exit):
        connection.run("false", hide=True)

    # If warn=True, it should return False instead of raising
    _, success = connection.run("false", warn=True, hide=True)
    assert success is False


def test_sudo_password_handling(mock_ssh_components):
    mock_session, mock_channel, mock_sock = mock_ssh_components
    
    with patch.dict("os.environ", {"MY_PASSWORD": "mypassword"}):
        host = HostConfig(
            domain_name="example.com", 
            user="testuser", 
            password_env="MY_PASSWORD"
        )
        conn = SSH2Connection(mock_session, host, mock_sock)
        
        # Simulate sudo prompt in output
        mock_channel.read.side_effect = [
            (16, b"[sudo] password:"), 
            (0, b"")
        ]
        
        conn.run("sudo ls", hide=True)
        
        # Verify password was written to channel
        mock_channel.write.assert_called_with(b"mypassword\n")
