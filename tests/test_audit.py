"""Tests for audit command."""

from __future__ import annotations

from unittest.mock import MagicMock, Mock, patch

import pytest

from fujin.commands.audit import Audit


# ============================================================================
# No Audit Logs
# ============================================================================


def test_audit_with_no_logs_shows_message():
    """audit with no logs shows 'No audit logs found' message."""
    mock_config = Mock()
    mock_config.app_name = "myapp"

    with (
        patch("fujin.commands.audit.read_logs", return_value=[]),
        patch("fujin.commands.audit.Console") as mock_console_class,
        patch.object(Audit, "config", mock_config),
        patch.object(Audit, "connection") as mock_conn_ctx,
    ):
        mock_conn = Mock()
        mock_conn_ctx.return_value.__enter__.return_value = mock_conn

        mock_console = MagicMock()
        mock_console_class.return_value = mock_console

        audit = Audit()
        audit()

        # Should print message
        mock_console.print.assert_called_once_with("[dim]No audit logs found[/dim]")


# ============================================================================
# Single Host
# ============================================================================


@pytest.mark.parametrize(
    "record,expected_patterns",
    [
        # Deploy operation
        (
            {
                "timestamp": "2024-01-15T10:30:00",
                "operation": "deploy",
                "user": "testuser",
                "host": "example.com",
                "app_name": "myapp",
                "version": "1.2.0",
            },
            ["example.com", "Deployed myapp version", "1.2.0", "testuser"],
        ),
        # Deploy with git commit
        (
            {
                "timestamp": "2024-01-15T10:30:00",
                "operation": "deploy",
                "user": "testuser",
                "host": "example.com",
                "app_name": "myapp",
                "version": "1.2.0",
                "git_commit": "abc1234567890",
            },
            ["example.com", "Deployed myapp version", "1.2.0", "abc1234"],
        ),
        # Rollback operation
        (
            {
                "timestamp": "2024-01-15T11:00:00",
                "operation": "rollback",
                "user": "testuser",
                "host": "example.com",
                "app_name": "myapp",
                "from_version": "1.2.0",
                "to_version": "1.1.0",
            },
            ["Rolled back myapp from", "1.2.0", "1.1.0"],
        ),
        # Down operation
        (
            {
                "timestamp": "2024-01-15T12:00:00",
                "operation": "down",
                "user": "testuser",
                "host": "example.com",
                "app_name": "myapp",
                "version": "1.2.0",
            },
            ["Stopped myapp version", "1.2.0"],
        ),
        # Full-down operation
        (
            {
                "timestamp": "2024-01-15T12:00:00",
                "operation": "full-down",
                "user": "testuser",
                "host": "example.com",
                "app_name": "myapp",
                "version": "1.2.0",
            },
            ["Stopped myapp version", "1.2.0", "full cleanup"],
        ),
        # Unknown operation
        (
            {
                "timestamp": "2024-01-15T13:00:00",
                "operation": "custom_operation",
                "user": "testuser",
                "host": "example.com",
                "app_name": "myapp",
            },
            ["custom_operation"],
        ),
    ],
)
def test_audit_displays_operations(record, expected_patterns):
    """audit displays different operation types correctly."""
    mock_config = Mock()
    mock_config.app_name = "myapp"

    with (
        patch("fujin.commands.audit.read_logs", return_value=[record]),
        patch("fujin.commands.audit.Console") as mock_console_class,
        patch.object(Audit, "config", mock_config),
        patch.object(Audit, "connection") as mock_conn_ctx,
    ):
        mock_conn = Mock()
        mock_conn_ctx.return_value.__enter__.return_value = mock_conn

        mock_console = MagicMock()
        mock_console_class.return_value = mock_console

        audit = Audit()
        audit()

        calls = [str(call) for call in mock_console.print.call_args_list]
        for pattern in expected_patterns:
            assert any(pattern in call for call in calls), (
                f"Pattern '{pattern}' not found in output"
            )


# ============================================================================
# Multiple Hosts
# ============================================================================


def test_audit_groups_by_host():
    """audit groups records by host."""
    records = [
        {
            "timestamp": "2024-01-15T10:00:00",
            "operation": "deploy",
            "user": "testuser",
            "host": "server1.com",
            "app_name": "myapp",
            "version": "1.0.0",
        },
        {
            "timestamp": "2024-01-15T11:00:00",
            "operation": "deploy",
            "user": "testuser",
            "host": "server2.com",
            "app_name": "myapp",
            "version": "1.0.0",
        },
        {
            "timestamp": "2024-01-15T12:00:00",
            "operation": "deploy",
            "user": "testuser",
            "host": "server1.com",
            "app_name": "myapp",
            "version": "1.1.0",
        },
    ]

    mock_config = Mock()
    mock_config.app_name = "myapp"

    with (
        patch("fujin.commands.audit.read_logs", return_value=records),
        patch("fujin.commands.audit.Console") as mock_console_class,
        patch.object(Audit, "config", mock_config),
        patch.object(Audit, "connection") as mock_conn_ctx,
    ):
        mock_conn = Mock()
        mock_conn_ctx.return_value.__enter__.return_value = mock_conn

        mock_console = MagicMock()
        mock_console_class.return_value = mock_console

        audit = Audit()
        audit()

        # Check both hosts are printed
        calls = [str(call) for call in mock_console.print.call_args_list]
        assert any("server1.com" in call for call in calls)
        assert any("server2.com" in call for call in calls)


# ============================================================================
# Limit Parameter
# ============================================================================


@pytest.mark.parametrize("limit,expected", [(10, 10), (None, 20)])
def test_audit_limit_parameter(limit, expected):
    """audit respects limit parameter with default of 20."""
    mock_config = Mock()
    mock_config.app_name = "myapp"

    with (
        patch("fujin.commands.audit.read_logs", return_value=[]) as mock_read_logs,
        patch.object(Audit, "config", mock_config),
        patch.object(Audit, "connection") as mock_conn_ctx,
    ):
        mock_conn = Mock()
        mock_conn_ctx.return_value.__enter__.return_value = mock_conn

        audit = Audit()
        if limit is not None:
            audit.limit = limit
        audit()

        # Should call read_logs with correct limit
        mock_read_logs.assert_called_once_with(
            connection=mock_conn,
            app_name="myapp",
            limit=expected,
        )


# ============================================================================
# Edge Cases
# ============================================================================


@pytest.mark.parametrize(
    "record,expected_behavior",
    [
        # Missing fields - should use defaults
        (
            {
                "timestamp": "2024-01-15T10:00:00",
                "operation": "deploy",
                # Missing user, host, app_name, version
            },
            "unknown",
        ),
        # Invalid timestamp - should use raw value
        (
            {
                "timestamp": "invalid-timestamp",
                "operation": "deploy",
                "user": "testuser",
                "host": "example.com",
                "app_name": "myapp",
                "version": "1.0.0",
            },
            "invalid-timestamp",
        ),
        # Missing timestamp - should use "unknown"
        (
            {
                # Missing timestamp
                "operation": "deploy",
                "user": "testuser",
                "host": "example.com",
                "app_name": "myapp",
                "version": "1.0.0",
            },
            "unknown",
        ),
        # Valid timestamp - should format correctly
        (
            {
                "timestamp": "2024-01-15T14:30:45.123456",
                "operation": "deploy",
                "user": "testuser",
                "host": "example.com",
                "app_name": "myapp",
                "version": "1.0.0",
            },
            "2024-01-15 14:30",
        ),
    ],
)
def test_audit_handles_edge_cases(record, expected_behavior):
    """audit handles missing/invalid fields and formats timestamps correctly."""
    mock_config = Mock()
    mock_config.app_name = "myapp"

    with (
        patch("fujin.commands.audit.read_logs", return_value=[record]),
        patch("fujin.commands.audit.Console") as mock_console_class,
        patch.object(Audit, "config", mock_config),
        patch.object(Audit, "connection") as mock_conn_ctx,
    ):
        mock_conn = Mock()
        mock_conn_ctx.return_value.__enter__.return_value = mock_conn

        mock_console = MagicMock()
        mock_console_class.return_value = mock_console

        audit = Audit()
        audit()

        # Should not raise and should contain expected behavior
        calls = [str(call) for call in mock_console.print.call_args_list]
        assert any(expected_behavior in call for call in calls)
