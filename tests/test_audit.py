"""Tests for audit command."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from fujin.commands.audit import Audit


# ============================================================================
# No Audit Logs
# ============================================================================


def test_audit_with_no_logs_shows_message():
    """audit with no logs shows 'No audit logs found' message."""
    with (
        patch("fujin.commands.audit.read_logs", return_value=[]),
        patch("fujin.commands.audit.Console") as mock_console_class,
    ):
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
                "details": {"app_name": "myapp", "version": "1.2.0"},
            },
            ["example.com", "Deployed myapp version", "1.2.0", "testuser"],
        ),
        # Rollback operation
        (
            {
                "timestamp": "2024-01-15T11:00:00",
                "operation": "rollback",
                "user": "testuser",
                "host": "example.com",
                "details": {
                    "app_name": "myapp",
                    "from_version": "1.2.0",
                    "to_version": "1.1.0",
                },
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
                "details": {"app_name": "myapp", "version": "1.2.0", "full": False},
            },
            ["Stopped myapp version", "1.2.0"],
        ),
        # Down operation with full flag
        (
            {
                "timestamp": "2024-01-15T12:00:00",
                "operation": "down",
                "user": "testuser",
                "host": "example.com",
                "details": {"app_name": "myapp", "version": "1.2.0", "full": True},
            },
            ["full cleanup"],
        ),
        # Unknown operation
        (
            {
                "timestamp": "2024-01-15T13:00:00",
                "operation": "custom_operation",
                "user": "testuser",
                "host": "example.com",
                "details": {"app_name": "myapp"},
            },
            ["custom_operation"],
        ),
    ],
)
def test_audit_displays_operations(record, expected_patterns):
    """audit displays different operation types correctly."""
    with (
        patch("fujin.commands.audit.read_logs", return_value=[record]),
        patch("fujin.commands.audit.Console") as mock_console_class,
    ):
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
            "details": {"app_name": "myapp", "version": "1.0.0"},
        },
        {
            "timestamp": "2024-01-15T11:00:00",
            "operation": "deploy",
            "user": "testuser",
            "host": "server2.com",
            "details": {"app_name": "myapp", "version": "1.0.0"},
        },
        {
            "timestamp": "2024-01-15T12:00:00",
            "operation": "deploy",
            "user": "testuser",
            "host": "server1.com",
            "details": {"app_name": "myapp", "version": "1.1.0"},
        },
    ]

    with (
        patch("fujin.commands.audit.read_logs", return_value=records),
        patch("fujin.commands.audit.Console") as mock_console_class,
    ):
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


def test_audit_respects_limit_parameter():
    """audit passes limit parameter to read_logs."""
    with patch("fujin.commands.audit.read_logs", return_value=[]) as mock_read_logs:
        audit = Audit(limit=10)
        audit()

        # Should call read_logs with limit
        mock_read_logs.assert_called_once_with(limit=10)


def test_audit_default_limit_is_20():
    """audit default limit is 20."""
    with patch("fujin.commands.audit.read_logs", return_value=[]) as mock_read_logs:
        audit = Audit()
        audit()

        # Should call read_logs with default limit
        mock_read_logs.assert_called_once_with(limit=20)


# ============================================================================
# Edge Cases
# ============================================================================


def test_audit_handles_missing_fields():
    """audit handles missing fields gracefully."""
    records = [
        {
            "timestamp": "2024-01-15T10:00:00",
            "operation": "deploy",
            # Missing user, host
            "details": {},  # Missing version, app_name
        }
    ]

    with (
        patch("fujin.commands.audit.read_logs", return_value=records),
        patch("fujin.commands.audit.Console") as mock_console_class,
    ):
        mock_console = MagicMock()
        mock_console_class.return_value = mock_console

        audit = Audit()
        audit()

        # Should not raise, should use defaults
        calls = [str(call) for call in mock_console.print.call_args_list]
        assert any("unknown" in call for call in calls)


def test_audit_handles_invalid_timestamp():
    """audit handles invalid timestamp format."""
    records = [
        {
            "timestamp": "invalid-timestamp",
            "operation": "deploy",
            "user": "testuser",
            "host": "example.com",
            "details": {"app_name": "myapp", "version": "1.0.0"},
        }
    ]

    with (
        patch("fujin.commands.audit.read_logs", return_value=records),
        patch("fujin.commands.audit.Console") as mock_console_class,
    ):
        mock_console = MagicMock()
        mock_console_class.return_value = mock_console

        audit = Audit()
        audit()

        # Should not raise, should use raw timestamp
        calls = [str(call) for call in mock_console.print.call_args_list]
        assert any("invalid-timestamp" in call for call in calls)


def test_audit_handles_missing_timestamp():
    """audit handles missing timestamp field."""
    records = [
        {
            # Missing timestamp
            "operation": "deploy",
            "user": "testuser",
            "host": "example.com",
            "details": {"app_name": "myapp", "version": "1.0.0"},
        }
    ]

    with (
        patch("fujin.commands.audit.read_logs", return_value=records),
        patch("fujin.commands.audit.Console") as mock_console_class,
    ):
        mock_console = MagicMock()
        mock_console_class.return_value = mock_console

        audit = Audit()
        audit()

        # Should not raise, should use "unknown"
        calls = [str(call) for call in mock_console.print.call_args_list]
        assert any("unknown" in call for call in calls)


def test_audit_formats_timestamp_correctly():
    """audit formats ISO timestamp to readable format."""
    records = [
        {
            "timestamp": "2024-01-15T14:30:45.123456",
            "operation": "deploy",
            "user": "testuser",
            "host": "example.com",
            "details": {"app_name": "myapp", "version": "1.0.0"},
        }
    ]

    with (
        patch("fujin.commands.audit.read_logs", return_value=records),
        patch("fujin.commands.audit.Console") as mock_console_class,
    ):
        mock_console = MagicMock()
        mock_console_class.return_value = mock_console

        audit = Audit()
        audit()

        # Should format as YYYY-MM-DD HH:MM
        calls = [str(call) for call in mock_console.print.call_args_list]
        assert any("2024-01-15 14:30" in call for call in calls)
