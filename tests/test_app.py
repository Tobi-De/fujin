"""Tests for app command - focused on resolve_units behavior and service operations."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import msgspec
import pytest

from fujin.commands.app import App
from fujin.config import Config


@pytest.fixture
def base_config(minimal_config_dict):
    """Base config for app tests."""
    minimal_config_dict["processes"]["worker"] = {"command": "celery", "replicas": 3}
    return minimal_config_dict


@pytest.fixture
def config_with_socket(minimal_config_dict):
    """Config with socket-activated process."""
    minimal_config_dict["processes"]["web"]["socket"] = True
    return minimal_config_dict


@pytest.fixture
def config_with_timer(minimal_config_dict):
    """Config with timer-based process."""
    minimal_config_dict["processes"]["health"] = {
        "command": "healthcheck",
        "replicas": 1,
        "timer": "daily",
    }
    return minimal_config_dict


@pytest.fixture
def config_with_all_features(minimal_config_dict):
    """Config with socket, timer, and multiple processes."""
    minimal_config_dict["processes"]["web"]["socket"] = True
    minimal_config_dict["processes"]["health"] = {
        "command": "healthcheck",
        "replicas": 1,
        "timer": "hourly",
    }
    return minimal_config_dict


# ============================================================================
# resolve_units - Basic Cases
# ============================================================================


def test_resolve_units_no_name_returns_all_units(base_config):
    """resolve_units with name=None returns all systemd units."""
    config = msgspec.convert(base_config, type=Config)

    with patch("fujin.config.Config.read", return_value=config):
        app = App()
        units = app._resolve_units(None)

    # Should return: testapp.service (web) and testapp-worker@1/2/3.service
    assert "testapp.service" in units
    assert "testapp-worker@1.service" in units
    assert "testapp-worker@2.service" in units
    assert "testapp-worker@3.service" in units
    assert len(units) == 4


def test_resolve_units_process_name_only(base_config):
    """resolve_units with just process name returns service units."""
    config = msgspec.convert(base_config, type=Config)

    with patch("fujin.config.Config.read", return_value=config):
        app = App()
        units = app._resolve_units("web")

    assert units == ["testapp.service"]


def test_resolve_units_process_with_replicas(base_config):
    """resolve_units returns all replica instances for multi-replica process."""
    config = msgspec.convert(base_config, type=Config)

    with patch("fujin.config.Config.read", return_value=config):
        app = App()
        units = app._resolve_units("worker")

    assert units == [
        "testapp-worker@1.service",
        "testapp-worker@2.service",
        "testapp-worker@3.service",
    ]


def test_resolve_units_invalid_process_name_raises_error(base_config):
    """resolve_units raises error for unknown process name."""
    config = msgspec.convert(base_config, type=Config)

    with patch("fujin.config.Config.read", return_value=config):
        app = App()

        with pytest.raises(SystemExit) as exc_info:
            app._resolve_units("invalid")

        assert exc_info.value.code == 1


# ============================================================================
# resolve_units - Suffix Handling
# ============================================================================


def test_resolve_units_with_service_suffix(base_config):
    """resolve_units with .service suffix returns only service units."""
    config = msgspec.convert(base_config, type=Config)

    with patch("fujin.config.Config.read", return_value=config):
        app = App()
        units = app._resolve_units("worker.service")

    # Should return worker service units only, not socket/timer
    assert units == [
        "testapp-worker@1.service",
        "testapp-worker@2.service",
        "testapp-worker@3.service",
    ]


def test_resolve_units_socket_suffix_returns_socket(config_with_socket):
    """resolve_units with .socket suffix returns socket unit."""
    config = msgspec.convert(config_with_socket, type=Config)

    with patch("fujin.config.Config.read", return_value=config):
        app = App()
        units = app._resolve_units("web.socket")

    assert units == ["testapp.socket"]


def test_resolve_units_timer_suffix_returns_timer(config_with_timer):
    """resolve_units with .timer suffix returns timer unit."""
    config = msgspec.convert(config_with_timer, type=Config)

    with patch("fujin.config.Config.read", return_value=config):
        app = App()
        units = app._resolve_units("health.timer")

    assert units == ["testapp-health.timer"]


@pytest.mark.parametrize(
    "config_fixture,name_with_suffix",
    [
        ("base_config", "web.socket"),
        ("base_config", "web.timer"),
    ],
)
def test_resolve_units_suffix_errors(config_fixture, name_with_suffix, request):
    """resolve_units raises error when suffix doesn't match process configuration."""
    config_dict = request.getfixturevalue(config_fixture)
    config = msgspec.convert(config_dict, type=Config)

    with patch("fujin.config.Config.read", return_value=config):
        app = App()

        with pytest.raises(SystemExit) as exc_info:
            app._resolve_units(name_with_suffix)

        assert exc_info.value.code == 1


# ============================================================================
# resolve_units - Special Keywords
# ============================================================================


def test_resolve_units_timer_keyword_returns_all_timers(config_with_all_features):
    """resolve_units with 'timer' keyword returns all timer units."""
    config = msgspec.convert(config_with_all_features, type=Config)

    with patch("fujin.config.Config.read", return_value=config):
        app = App()
        units = app._resolve_units("timer")

    assert "testapp-health.timer" in units
    assert len(units) == 1


def test_resolve_units_socket_keyword_returns_socket(config_with_socket):
    """resolve_units with 'socket' keyword returns socket unit if any process has socket."""
    config = msgspec.convert(config_with_socket, type=Config)

    with patch("fujin.config.Config.read", return_value=config):
        app = App()
        units = app._resolve_units("socket")

    assert units == ["testapp.socket"]


def test_resolve_units_socket_keyword_returns_empty_without_socket(base_config):
    """resolve_units with 'socket' keyword returns empty list if no socket configured."""
    config = msgspec.convert(base_config, type=Config)

    with patch("fujin.config.Config.read", return_value=config):
        app = App()
        units = app._resolve_units("socket")

    assert units == []


# ============================================================================
# resolve_units - use_templates Flag
# ============================================================================


def test_resolve_units_use_templates_true_returns_template_names(base_config):
    """resolve_units with use_templates=True returns template names."""
    config = msgspec.convert(base_config, type=Config)

    with patch("fujin.config.Config.read", return_value=config):
        app = App()
        units = app._resolve_units("worker", use_templates=True)

    # Should return template name, not instance names
    assert units == ["testapp-worker@.service"]


def test_resolve_units_use_templates_false_returns_instance_names(base_config):
    """resolve_units with use_templates=False returns instance names."""
    config = msgspec.convert(base_config, type=Config)

    with patch("fujin.config.Config.read", return_value=config):
        app = App()
        units = app._resolve_units("worker", use_templates=False)

    # Should return instance names
    assert units == [
        "testapp-worker@1.service",
        "testapp-worker@2.service",
        "testapp-worker@3.service",
    ]


def test_resolve_units_single_replica_same_result_regardless_of_templates(base_config):
    """resolve_units for single replica process returns same result with/without templates."""
    config = msgspec.convert(base_config, type=Config)

    with patch("fujin.config.Config.read", return_value=config):
        app = App()
        units_template = app._resolve_units("web", use_templates=True)
        units_instance = app._resolve_units("web", use_templates=False)

    # Single replica has same name either way
    assert units_template == units_instance == ["testapp.service"]


# ============================================================================
# resolve_units - Process with Socket/Timer
# ============================================================================


def test_resolve_units_process_with_socket_includes_socket(config_with_socket):
    """resolve_units for process with socket returns both service and socket."""
    config = msgspec.convert(config_with_socket, type=Config)

    with patch("fujin.config.Config.read", return_value=config):
        app = App()
        units = app._resolve_units("web")

    assert "testapp.service" in units
    assert "testapp.socket" in units
    assert len(units) == 2


def test_resolve_units_process_with_timer_includes_timer(config_with_timer):
    """resolve_units for process with timer returns both service and timer."""
    config = msgspec.convert(config_with_timer, type=Config)

    with patch("fujin.config.Config.read", return_value=config):
        app = App()
        units = app._resolve_units("health")

    # Should have service + timer
    assert "testapp-health.service" in units
    assert "testapp-health.timer" in units
    assert len(units) == 2


def test_resolve_units_service_suffix_excludes_socket_and_timer(
    config_with_all_features,
):
    """resolve_units with .service suffix excludes socket and timer even if configured."""
    config = msgspec.convert(config_with_all_features, type=Config)

    with patch("fujin.config.Config.read", return_value=config):
        app = App()
        units = app._resolve_units("health.service")

    # Should only return service units, not timer
    assert all(".service" in u for u in units)
    assert not any(".timer" in u for u in units)
    assert units == ["testapp-health.service"]


# ============================================================================
# Service Commands Integration
# ============================================================================


def test_start_command_uses_resolve_units_with_templates_false(base_config):
    """app start uses resolve_units with use_templates=False."""
    config = msgspec.convert(base_config, type=Config)
    mock_conn = MagicMock()
    mock_conn.run.return_value = ("", True)

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch.object(App, "connection") as mock_connection,
        patch.object(App, "output", MagicMock()),
    ):
        mock_connection.return_value.__enter__.return_value = mock_conn
        mock_connection.return_value.__exit__.return_value = None

        app = App()
        app.start("worker")

        # Verify systemctl was called with instance names (not template)
        calls = [call[0][0] for call in mock_conn.run.call_args_list]
        assert any("systemctl start" in cmd for cmd in calls)
        assert any("testapp-worker@1.service" in cmd for cmd in calls)
        assert not any("testapp-worker@.service" in cmd for cmd in calls)


def test_start_command_with_no_name_starts_all_services(base_config):
    """app start with no name starts all services."""
    config = msgspec.convert(base_config, type=Config)
    mock_conn = MagicMock()
    mock_conn.run.return_value = ("", True)

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch.object(App, "connection") as mock_connection,
        patch.object(App, "output", MagicMock()),
    ):
        mock_connection.return_value.__enter__.return_value = mock_conn
        mock_connection.return_value.__exit__.return_value = None

        app = App()
        app.start(None)

        # Verify all units were included
        calls = [call[0][0] for call in mock_conn.run.call_args_list]
        cmd = calls[0]
        assert "testapp.service" in cmd
        assert "testapp-worker@1.service" in cmd
        assert "testapp-worker@2.service" in cmd
        assert "testapp-worker@3.service" in cmd


def test_logs_command_uses_resolve_units_with_templates_false(base_config):
    """app logs uses resolve_units with use_templates=False."""
    config = msgspec.convert(base_config, type=Config)
    mock_conn = MagicMock()
    mock_conn.run.return_value = ("", True)

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch.object(App, "connection") as mock_connection,
        patch.object(App, "output", MagicMock()),
    ):
        mock_connection.return_value.__enter__.return_value = mock_conn
        mock_connection.return_value.__exit__.return_value = None

        app = App()
        app.logs("worker", follow=False, lines=50, level=None, since=None, grep=None)

        calls = [call[0][0] for call in mock_conn.run.call_args_list]
        # Should use instance names in journalctl
        assert any("-u testapp-worker@1.service" in cmd for cmd in calls)
        assert any("-u testapp-worker@2.service" in cmd for cmd in calls)


def test_cat_command_uses_resolve_units_with_templates_true(base_config):
    """app cat uses resolve_units with use_templates=True."""
    config = msgspec.convert(base_config, type=Config)
    mock_conn = MagicMock()
    mock_conn.run.return_value = ("", True)

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch.object(App, "connection") as mock_connection,
        patch.object(App, "output", MagicMock()),
    ):
        mock_connection.return_value.__enter__.return_value = mock_conn
        mock_connection.return_value.__exit__.return_value = None

        app = App()
        app.cat("worker")

        calls = [call[0][0] for call in mock_conn.run.call_args_list]
        # Should use template name for cat
        assert any("systemctl cat testapp-worker@.service" in cmd for cmd in calls)
