"""Tests for SSH connection wrapper."""

from __future__ import annotations

import os
import re
import shlex
import socket
from unittest.mock import MagicMock, patch

import cappa
import pytest
from ssh2.exceptions import SSH2Error

from fujin.config import HostConfig
from fujin.connection import SPOOL_THRESHOLD_BYTES, SSH2Connection, should_spool
from fujin.errors import CommandError, ConnectionError, UploadError
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
# Spooling large commands (never send big scripts through the exec channel)
# ============================================================================


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        (0, False),
        (SPOOL_THRESHOLD_BYTES, False),
        (SPOOL_THRESHOLD_BYTES + 1, True),
    ],
)
def test_should_spool_uses_the_byte_threshold(size, expected):
    assert should_spool("x" * size) is expected


def test_should_spool_counts_utf8_bytes():
    # 2048 chars * 2 bytes == exactly the threshold; one more char spools.
    assert should_spool("é" * 2048) is False
    assert should_spool("é" * 2049) is True


@pytest.fixture
def spool_connection(connection):
    """Connection with the channel internals replaced by recording doubles."""
    connection.put = MagicMock()
    execute = MagicMock(return_value=("output", True))
    connection._execute = execute
    return connection, execute


def test_large_non_pty_command_is_spooled_then_cleaned_up(spool_connection):
    conn, execute = spool_connection
    big = "echo " + "x" * (SPOOL_THRESHOLD_BYTES + 100)
    captured = {}

    def fake_put(local, remote, verify=False):
        captured["local"] = local
        captured["remote"] = remote
        with open(local, encoding="utf-8") as handle:
            captured["content"] = handle.read()
        captured["mode"] = os.stat(local).st_mode & 0o777

    conn.put.side_effect = fake_put

    assert conn.run(big, warn=True, hide=True, idle_timeout=12) == ("output", True)

    remote = captured["remote"]
    assert re.fullmatch(r"/tmp/fujin-cmd-\d+-[0-9a-f]{16}\.sh", remote)
    assert captured["mode"] == 0o600
    assert captured["content"].startswith('export PATH="')
    assert captured["content"].endswith(big)
    assert not os.path.exists(captured["local"])

    assert execute.call_count == 2
    run_call, cleanup_call = execute.call_args_list
    assert run_call.args[0] == f"sh {shlex.quote(remote)}"
    assert run_call.args[1:] == (big, True, False, True, 12)
    assert f"rm -f {shlex.quote(remote)}" in cleanup_call.args[0]
    assert cleanup_call.args[2] is True  # cleanup is warn=True: never raises


def test_spool_path_is_unique_per_call(spool_connection):
    conn, _ = spool_connection
    big = "echo " + "x" * SPOOL_THRESHOLD_BYTES
    remotes = []
    conn.put.side_effect = lambda local, remote, verify=False: remotes.append(remote)

    conn.run(big, hide=True, warn=True)
    conn.run(big, hide=True, warn=True)

    assert len(set(remotes)) == 2


def test_small_non_pty_command_is_not_spooled(spool_connection):
    conn, execute = spool_connection

    assert conn.run("echo hi", hide=True) == ("output", True)

    conn.put.assert_not_called()
    assert execute.call_count == 1
    assert execute.call_args.args[0].endswith("; echo hi")


def test_pty_command_is_never_spooled_even_when_large(connection, mock_ssh_components):
    _, mock_channel, _ = mock_ssh_components
    connection.put = MagicMock()
    big = "echo " + "x" * SPOOL_THRESHOLD_BYTES

    connection.run(big, pty=True, hide=True)

    connection.put.assert_not_called()
    mock_channel.execute.assert_called_once()
    assert big in mock_channel.execute.call_args.args[0]


def test_spooled_cleanup_runs_when_execution_fails(spool_connection):
    conn, execute = spool_connection

    original = ConnectionError("transport lost")

    def fake_execute(directive, command, warn, pty, hide, idle_timeout):
        if directive.startswith("sh "):
            raise original
        return ("", True)

    execute.side_effect = fake_execute

    with pytest.raises(ConnectionError) as excinfo:
        conn.run("echo " + "x" * SPOOL_THRESHOLD_BYTES, hide=True)

    assert excinfo.value is original

    assert execute.call_count == 2
    assert "rm -f /tmp/fujin-cmd-" in execute.call_args_list[1].args[0]


def test_spooled_cleanup_failure_does_not_mask_original_error(spool_connection):
    conn, execute = spool_connection

    def fake_execute(directive, command, warn, pty, hide, idle_timeout):
        raise RuntimeError(f"boom: {directive.split()[0]}")

    execute.side_effect = fake_execute

    with pytest.raises(RuntimeError, match="boom: sh"):
        conn.run("echo " + "x" * SPOOL_THRESHOLD_BYTES, hide=True)

    # The rm ran (and failed) without replacing the original error.
    assert execute.call_count == 2


def test_spooled_command_is_cleaned_up_when_upload_fails(spool_connection):
    conn, execute = spool_connection
    captured = {}

    upload_error = UploadError("scp failed")

    def failing_put(local, remote, verify=False):
        captured["local"] = local
        captured["remote"] = remote
        raise upload_error

    conn.put.side_effect = failing_put

    with pytest.raises(UploadError) as excinfo:
        conn.run("echo " + "x" * SPOOL_THRESHOLD_BYTES, hide=True)

    assert excinfo.value is upload_error

    assert not os.path.exists(captured["local"])
    assert execute.call_count == 1
    assert f"rm -f {shlex.quote(captured['remote'])}" in execute.call_args.args[0]


def test_spool_upload_transport_failure_is_connection_error(spool_connection):
    conn, _ = spool_connection
    conn.put.side_effect = SSH2Error("socket disconnected")

    with pytest.raises(ConnectionError) as excinfo:
        conn.run("echo " + "x" * SPOOL_THRESHOLD_BYTES, hide=True)

    assert excinfo.value.code == EXIT_HOST_UNREACHABLE


# ============================================================================
