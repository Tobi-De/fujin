"""Tests for app command - focusing on name resolution."""

from __future__ import annotations

from unittest.mock import patch

import cappa
import msgspec
import pytest

from fujin.commands.app import App
from fujin.config import Config


@pytest.fixture
def app_test_env(tmp_path, monkeypatch):
    """Set up test environment with service files."""
    monkeypatch.chdir(tmp_path)

    # Create .fujin/systemd directory structure
    fujin_dir = tmp_path / ".fujin"
    systemd_dir = fujin_dir / "systemd"
    systemd_dir.mkdir(parents=True)

    # Create web service (single replica)
    (systemd_dir / "web.service").write_text("""[Unit]
Description=Web server

[Service]
ExecStart=/usr/bin/python -m app

[Install]
WantedBy=multi-user.target
""")

    # Create worker service (template for multiple replicas)
    (systemd_dir / "worker@.service").write_text("""[Unit]
Description=Worker %i

[Service]
ExecStart=/usr/bin/python -m worker

[Install]
WantedBy=multi-user.target
""")

    # Create api service with socket and timer
    (systemd_dir / "api.service").write_text("""[Unit]
Description=API

[Service]
ExecStart=/usr/bin/python -m api

[Install]
WantedBy=multi-user.target
""")

    (systemd_dir / "api.socket").write_text("""[Unit]
Description=API socket

[Socket]
ListenStream=/run/api.sock

[Install]
WantedBy=sockets.target
""")

    (systemd_dir / "api.timer").write_text("""[Unit]
Description=API timer

[Timer]
OnCalendar=daily

[Install]
WantedBy=timers.target
""")

    # Create config
    config_dict = {
        "app": "testapp",
        "version": "1.0.0",
        "build_command": "echo building",
        "installation_mode": "python-package",
        "python_version": "3.11",
        "distfile": "dist/testapp-{version}-py3-none-any.whl",
        "hosts": [{"address": "example.com", "user": "deploy"}],
        "replicas": {"worker": 3},  # Worker has 3 replicas
    }

    return msgspec.convert(config_dict, type=Config)


# Name Resolution Tests


def test_resolve_single_replica_service_templates(app_test_env):
    """Single replica service resolves to template name when use_templates=True."""
    with patch("fujin.config.Config.read", return_value=app_test_env):
        app = App()
        result = app._resolve_units("web", use_templates=True)
        assert result == ["testapp-web.service"]


def test_resolve_single_replica_service_instances(app_test_env):
    """Single replica service resolves to same name when use_templates=False."""
    with patch("fujin.config.Config.read", return_value=app_test_env):
        app = App()
        result = app._resolve_units("web", use_templates=False)
        assert result == ["testapp-web.service"]


def test_resolve_multi_replica_service_template(app_test_env):
    """Multi-replica service resolves to template name with @ when use_templates=True."""
    with patch("fujin.config.Config.read", return_value=app_test_env):
        app = App()
        result = app._resolve_units("worker", use_templates=True)
        assert result == ["testapp-worker@.service"]


def test_resolve_multi_replica_service_instances(app_test_env):
    """Multi-replica service resolves to all instance names when use_templates=False."""
    with patch("fujin.config.Config.read", return_value=app_test_env):
        app = App()
        result = app._resolve_units("worker", use_templates=False)
        assert result == [
            "testapp-worker@1.service",
            "testapp-worker@2.service",
            "testapp-worker@3.service",
        ]


def test_resolve_with_service_suffix(app_test_env):
    """Service name with .service suffix works correctly."""
    with patch("fujin.config.Config.read", return_value=app_test_env):
        app = App()
        result = app._resolve_units("web.service", use_templates=True)
        assert result == ["testapp-web.service"]


def test_resolve_socket_suffix(app_test_env):
    """Socket suffix resolves to socket unit name."""
    with patch("fujin.config.Config.read", return_value=app_test_env):
        app = App()
        result = app._resolve_units("api.socket", use_templates=True)
        assert result == ["testapp-api.socket"]


def test_resolve_timer_suffix(app_test_env):
    """Timer suffix resolves to timer unit name."""
    with patch("fujin.config.Config.read", return_value=app_test_env):
        app = App()
        result = app._resolve_units("api.timer", use_templates=True)
        assert result == ["testapp-api.timer"]


def test_resolve_socket_on_service_without_socket_fails(app_test_env):
    """Requesting socket for service without socket fails with clear error."""
    with patch("fujin.config.Config.read", return_value=app_test_env):
        app = App()
        with pytest.raises(cappa.Exit) as exc_info:
            app._resolve_units("web.socket", use_templates=True)

        assert exc_info.value.code == 1
        assert "does not have a socket" in str(exc_info.value.message)


def test_resolve_timer_on_service_without_timer_fails(app_test_env):
    """Requesting timer for service without timer fails with clear error."""
    with patch("fujin.config.Config.read", return_value=app_test_env):
        app = App()
        with pytest.raises(cappa.Exit) as exc_info:
            app._resolve_units("web.timer", use_templates=True)

        assert exc_info.value.code == 1
        assert "does not have a timer" in str(exc_info.value.message)


def test_resolve_nonexistent_service_fails(app_test_env):
    """Requesting nonexistent service fails with clear error."""
    with patch("fujin.config.Config.read", return_value=app_test_env):
        app = App()
        with pytest.raises(cappa.Exit) as exc_info:
            app._resolve_units("nonexistent", use_templates=True)

        assert exc_info.value.code == 1
        assert "unknown" in str(exc_info.value.message).lower()


def test_resolve_none_returns_all_units(app_test_env):
    """Passing None returns all systemd unit names."""
    with patch("fujin.config.Config.read", return_value=app_test_env):
        app = App()
        result = app._resolve_units(None, use_templates=True)

        # Should have web, worker instances (not template), api
        assert "testapp-web.service" in result
        assert "testapp-api.service" in result
        # Worker will be instances since that's what systemd_units returns
        assert any("testapp-worker@" in r for r in result)


# Helper Method Tests


def test_format_status_active():
    """Active status formatted in green."""
    app = App()
    result = app._format_status("active")
    assert "[bold green]" in result
    assert "active" in result


def test_format_status_failed():
    """Failed status formatted in red."""
    app = App()
    result = app._format_status("failed")
    assert "[bold red]" in result
    assert "failed" in result


def test_format_status_inactive():
    """Inactive status formatted as dim."""
    app = App()
    result = app._format_status("inactive")
    assert "[dim]" in result
    assert "inactive" in result


def test_format_status_all_replicas_running():
    """All replicas running shows green."""
    app = App()
    result = app._format_status("3/3")
    assert "[bold green]" in result
    assert "3/3" in result


def test_format_status_some_replicas_running():
    """Some replicas running shows yellow."""
    app = App()
    result = app._format_status("2/3")
    assert "[bold yellow]" in result
    assert "2/3" in result


def test_format_status_no_replicas_running():
    """No replicas running shows red."""
    app = App()
    result = app._format_status("0/3")
    assert "[bold red]" in result
    assert "0/3" in result


def test_find_dropins_no_dropins(app_test_env):
    """Returns empty list when no dropins exist."""
    with patch("fujin.config.Config.read", return_value=app_test_env):
        app = App()
        deployed_unit = app.config.deployed_units[0]
        result = app._find_dropins(deployed_unit)
        assert result == []


def test_find_dropins_with_common(app_test_env, tmp_path):
    """Finds common dropins."""
    common_dir = tmp_path / ".fujin" / "systemd" / "common.d"
    common_dir.mkdir(parents=True)
    (common_dir / "limits.conf").write_text("[Service]\nLimitNOFILE=65536")

    with patch("fujin.config.Config.read", return_value=app_test_env):
        app = App()
        deployed_unit = app.config.deployed_units[0]
        result = app._find_dropins(deployed_unit)

        assert len(result) == 1
        assert "common.d/limits.conf" in result


def test_find_dropins_with_service_specific(app_test_env, tmp_path):
    """Finds service-specific dropins."""
    service_dir = tmp_path / ".fujin" / "systemd" / "web.service.d"
    service_dir.mkdir(parents=True)
    (service_dir / "override.conf").write_text("[Service]\nEnvironment=DEBUG=1")

    with patch("fujin.config.Config.read", return_value=app_test_env):
        app = App()
        deployed_unit = [
            u for u in app.config.deployed_units if u.service_name == "web"
        ][0]
        result = app._find_dropins(deployed_unit)

        assert len(result) == 1
        assert "web.service.d/override.conf" in result


def test_get_available_options(app_test_env):
    """Returns formatted list of available options."""
    with patch("fujin.config.Config.read", return_value=app_test_env):
        app = App()
        result = app._get_available_options()

        # Check special keywords
        assert "caddy" in result
        assert "env" in result
        assert "units" in result

        # Check service names
        assert "web" in result
        assert "worker" in result
        assert "api" in result

        # Check socket and timer
        assert "api.socket" in result
        assert "api.timer" in result
