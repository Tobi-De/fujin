"""Tests for SSH connection wrapper."""

from __future__ import annotations

import socket
from unittest.mock import MagicMock, patch

import cappa
import pytest
from ssh2.exceptions import SSH2Error

from fujin.config import HostConfig
from fujin.connection import SSH2Connection
from fujin.errors import CommandError, ConnectionError
from fujin.exit_codes import EXIT_HOST_UNREACHABLE


@pytest.fixture
def mock_ssh_components():
    """Mock ssh2-python Session, Channel, and Socket."""
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
        mock_channel.read.return_value = (0, b"")
        mock_channel.read_stderr.return_value = (0, b"")
        mock_channel.get_exit_status.return_value = 0

        # Default select behavior
        mock_select.return_value = ([], [], [])

        yield mock_session, mock_channel, mock_sock


@pytest.fixture
def connection(mock_ssh_components):
    mock_session, _, mock_sock = mock_ssh_components
    host = HostConfig(address="example.com", user="testuser")
    return SSH2Connection(mock_session, host, mock_sock)


# ============================================================================
# Command Construction
# ============================================================================


def test_command_includes_path_export(connection, mock_ssh_components):
    _, mock_channel, _ = mock_ssh_components

    connection.run("echo hello", hide=True)

    # Should prepend PATH with .cargo/bin and .local/bin
    expected = 'export PATH="/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH"; echo hello'
    mock_channel.execute.assert_called_with(expected)


def test_command_includes_cd_when_in_directory(connection, mock_ssh_components):
    _, mock_channel, _ = mock_ssh_components

    connection.cwd = "/var/www"
    connection.run("ls", hide=True)

    expected = 'export PATH="/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH"; cd /var/www && ls'
    mock_channel.execute.assert_called_with(expected)


# ============================================================================
# cd() Context Manager
# ============================================================================


def test_cd_context_manager_with_absolute_and_relative_paths(
    connection, mock_ssh_components
):
    _, mock_channel, _ = mock_ssh_components

    # Absolute path sets cwd directly
    with connection.cd("/var/www"):
        connection.run("ls", hide=True)
        assert connection.cwd == "/var/www"

        expected_1 = 'export PATH="/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH"; cd /var/www && ls'
        mock_channel.execute.assert_called_with(expected_1)

        # Relative path appends to current cwd
        with connection.cd("html"):
            connection.run("touch index.html", hide=True)
            assert connection.cwd == "/var/www/html"

            expected_2 = 'export PATH="/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH"; cd /var/www/html && touch index.html'
            mock_channel.execute.assert_called_with(expected_2)

    # Should restore to empty cwd after exiting context
    assert connection.cwd == ""
    connection.run("whoami", hide=True)
    expected_3 = 'export PATH="/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH"; whoami'
    mock_channel.execute.assert_called_with(expected_3)


def test_cd_with_relative_path_from_empty_cwd(connection, mock_ssh_components):
    _, mock_channel, _ = mock_ssh_components

    # Starting from empty cwd, relative path becomes the cwd
    with connection.cd("projects"):
        assert connection.cwd == "projects"
        connection.run("ls", hide=True)

        expected = 'export PATH="/home/testuser/.cargo/bin:/home/testuser/.local/bin:$PATH"; cd projects && ls'
        mock_channel.execute.assert_called_with(expected)


# ============================================================================
# Return Values and Error Handling
# ============================================================================


def test_run_returns_stdout_and_success_flag(connection, mock_ssh_components):
    _, mock_channel, _ = mock_ssh_components

    # Simulate successful command with output
    mock_channel.read.side_effect = [(5, b"hello"), (0, b"")]
    mock_channel.get_exit_status.return_value = 0

    stdout, success = connection.run("echo hello", hide=True)

    assert stdout == "hello"
    assert success is True


def test_run_raises_on_nonzero_exit_by_default(connection, mock_ssh_components):
    _, mock_channel, _ = mock_ssh_components

    mock_channel.get_exit_status.return_value = 1

    with pytest.raises(cappa.Exit):
        connection.run("false", hide=True)


def test_run_returns_false_with_warn_parameter(connection, mock_ssh_components):
    _, mock_channel, _ = mock_ssh_components

    mock_channel.get_exit_status.return_value = 1

    stdout, success = connection.run("false", warn=True, hide=True)

    assert success is False


def test_hide_parameter_accepts_out_and_err_values(connection, mock_ssh_components):
    _, mock_channel, _ = mock_ssh_components

    mock_channel.read.side_effect = [(5, b"hello"), (0, b"")]
    mock_channel.read_stderr.side_effect = [(5, b"error"), (0, b"")]

    # hide="out" - command should still execute and return output
    stdout, success = connection.run("echo hello", hide="out")
    assert stdout == "hello"
    assert success is True

    # Reset for next test
    mock_channel.read.side_effect = [(5, b"world"), (0, b"")]
    mock_channel.read_stderr.side_effect = [(0, b"")]

    # hide="err" - command should still execute and return output
    stdout, success = connection.run("echo world", hide="err")
    assert stdout == "world"
    assert success is True


# ============================================================================
# Transport loss -> exit code 5 (never CommandError)
# ============================================================================


def test_wedged_channel_raises_connection_error(connection, mock_ssh_components):
    """A channel that makes no progress is transport loss (5), not a
    negative remote exit status."""
    _, mock_channel, _ = mock_ssh_components
    mock_channel.eof.return_value = False

    with pytest.raises(ConnectionError) as excinfo:
        connection.run("sleep forever", hide=True, idle_timeout=0)

    assert excinfo.value.code == EXIT_HOST_UNREACHABLE
    # Cleanup still closed the channel.
    mock_channel.close.assert_called()


def test_mid_command_eof_with_negative_exit_status_is_connection_error(
    connection, mock_ssh_components
):
    """libssh2 reports a negative status when the channel died before the
    remote exit status arrived: that is transport loss."""
    _, mock_channel, _ = mock_ssh_components
    mock_channel.get_exit_status.return_value = -1

    with pytest.raises(ConnectionError) as excinfo:
        connection.run("echo hi", hide=True)

    assert excinfo.value.code == EXIT_HOST_UNREACHABLE


def test_libssh2_failure_while_reading_is_connection_error(
    connection, mock_ssh_components
):
    _, mock_channel, _ = mock_ssh_components
    mock_channel.read.side_effect = SSH2Error("socket disconnected")

    with pytest.raises(ConnectionError) as excinfo:
        connection.run("echo hi", hide=True)

    assert excinfo.value.code == EXIT_HOST_UNREACHABLE


def test_cleanup_failure_does_not_mask_transport_error(connection, mock_ssh_components):
    """The finally-block cleanup is best effort: a throwing close must not
    replace the ConnectionError carrying code 5."""
    _, mock_channel, _ = mock_ssh_components
    mock_channel.read.side_effect = SSH2Error("socket disconnected")
    mock_channel.wait_closed.side_effect = RuntimeError("cleanup exploded")

    with pytest.raises(ConnectionError) as excinfo:
        connection.run("echo hi", hide=True)

    assert excinfo.value.code == EXIT_HOST_UNREACHABLE


def test_cleanup_failure_does_not_mask_remote_exit_status(
    connection, mock_ssh_components
):
    """A genuine remote non-zero exit keeps its status even when cleanup
    raises."""
    _, mock_channel, _ = mock_ssh_components
    mock_channel.get_exit_status.return_value = 7
    mock_channel.close.side_effect = RuntimeError("close exploded")

    with pytest.raises(CommandError) as excinfo:
        connection.run("false", hide=True)

    assert excinfo.value.code == 7


# ============================================================================
