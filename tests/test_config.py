"""Tests for configuration loading and validation."""

from __future__ import annotations

from pathlib import Path

import msgspec
import pytest

from fujin.config import (
    RESERVED_PROCESS_NAMES,
    Config,
    InstallationMode,
    ProcessConfig,
    RouteConfig,
    SiteConfig,
    TimerConfig,
    read_version_from_pyproject,
)
from fujin.errors import ImproperlyConfiguredError


# Fixtures are now imported from conftest.py


# ============================================================================
# Config Loading and Version Handling
# ============================================================================


def test_config_loads_with_explicit_version(minimal_config_dict):
    config = msgspec.convert(minimal_config_dict, type=Config)

    assert config.app_name == "testapp"
    assert config.version == "1.0.0"
    assert config.installation_mode == InstallationMode.PY_PACKAGE


def test_config_reads_version_from_pyproject_when_not_specified(
    minimal_config_dict, temp_project_dir
):
    del minimal_config_dict["version"]

    config = msgspec.convert(minimal_config_dict, type=Config)
    assert config.version == "2.5.0"


def test_read_version_from_pyproject_raises_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    with pytest.raises(Exception) as exc_info:  # msgspec.ValidationError
        read_version_from_pyproject()

    assert "version was not found" in str(exc_info.value).lower()


def test_read_version_from_pyproject_raises_when_version_key_missing(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'test'")

    with pytest.raises(Exception) as exc_info:
        read_version_from_pyproject()

    assert "version was not found" in str(exc_info.value).lower()


# ============================================================================
# Python Version Handling
# ============================================================================


def test_config_reads_python_version_from_file(minimal_config_dict, temp_project_dir):
    (temp_project_dir / ".python-version").write_text("3.11.5\n")
    del minimal_config_dict["python_version"]  # Remove to test file reading

    config = msgspec.convert(minimal_config_dict, type=Config)
    assert config.python_version == "3.11.5"


def test_config_uses_explicit_python_version(minimal_config_dict, temp_project_dir):
    (temp_project_dir / ".python-version").write_text("3.11.5\n")
    minimal_config_dict["python_version"] = "3.12.0"

    config = msgspec.convert(minimal_config_dict, type=Config)
    assert config.python_version == "3.12.0"


def test_config_raises_when_python_version_missing_for_python_package(
    minimal_config_dict, temp_project_dir
):
    del minimal_config_dict["python_version"]  # Remove to test missing behavior

    with pytest.raises(Exception) as exc_info:
        msgspec.convert(minimal_config_dict, type=Config)

    assert "python_version" in str(exc_info.value).lower()


def test_config_binary_mode_doesnt_require_python_version(minimal_config_dict):
    minimal_config_dict["installation_mode"] = "binary"

    config = msgspec.convert(minimal_config_dict, type=Config)
    assert config.installation_mode == InstallationMode.BINARY
    # python_version can be None for binary mode


# ============================================================================
# Host Configuration
# ============================================================================


def test_config_requires_at_least_one_host(minimal_config_dict):
    minimal_config_dict["hosts"] = []

    with pytest.raises(ImproperlyConfiguredError) as exc_info:
        msgspec.convert(minimal_config_dict, type=Config)

    assert "at least one host" in exc_info.value.message.lower()


def test_config_with_multiple_hosts_requires_names(minimal_config_dict):
    minimal_config_dict["hosts"] = [
        {"address": "host1.com", "user": "user1"},
        {"address": "host2.com", "user": "user2"},
    ]

    with pytest.raises(ImproperlyConfiguredError) as exc_info:
        msgspec.convert(minimal_config_dict, type=Config)

    assert "name" in exc_info.value.message.lower()


def test_config_with_multiple_named_hosts_succeeds(minimal_config_dict):
    minimal_config_dict["hosts"] = [
        {"name": "prod", "address": "host1.com", "user": "user1"},
        {"name": "staging", "address": "host2.com", "user": "user2"},
    ]

    config = msgspec.convert(minimal_config_dict, type=Config)
    assert len(config.hosts) == 2


def test_config_requires_unique_host_names(minimal_config_dict):
    minimal_config_dict["hosts"] = [
        {"name": "prod", "address": "host1.com", "user": "user1"},
        {"name": "prod", "address": "host2.com", "user": "user2"},
    ]

    with pytest.raises(ImproperlyConfiguredError) as exc_info:
        msgspec.convert(minimal_config_dict, type=Config)

    assert "unique" in exc_info.value.message.lower()


def test_select_host_returns_first_when_no_name_specified(minimal_config_dict):
    config = msgspec.convert(minimal_config_dict, type=Config)

    host = config.select_host()
    assert host == config.hosts[0]
    assert host.address == "example.com"


def test_select_host_by_name(minimal_config_dict):
    minimal_config_dict["hosts"] = [
        {"name": "prod", "address": "prod.com", "user": "user1"},
        {"name": "staging", "address": "staging.com", "user": "user2"},
    ]
    config = msgspec.convert(minimal_config_dict, type=Config)

    host = config.select_host("staging")
    assert host.name == "staging"
    assert host.address == "staging.com"


def test_select_host_raises_when_name_not_found(minimal_config_dict):
    minimal_config_dict["hosts"] = [
        {"name": "prod", "address": "prod.com", "user": "user1"},
    ]
    config = msgspec.convert(minimal_config_dict, type=Config)

    with pytest.raises(ImproperlyConfiguredError) as exc_info:
        config.select_host("nonexistent")

    assert "not found" in exc_info.value.message.lower()
    assert "prod" in exc_info.value.message  # Shows available hosts


# ============================================================================
# Process Configuration Validation
# ============================================================================


def test_config_requires_at_least_one_process(minimal_config_dict):
    minimal_config_dict["processes"] = {}

    with pytest.raises(ImproperlyConfiguredError) as exc_info:
        msgspec.convert(minimal_config_dict, type=Config)

    assert "at least one process" in exc_info.value.message.lower()


@pytest.mark.parametrize(
    "process_name,expected_error",
    [
        ("", "empty"),
        ("my worker", "spaces"),
    ]
    + [(name, "reserved") for name in RESERVED_PROCESS_NAMES],
)
def test_config_validates_process_names(
    minimal_config_dict, process_name, expected_error
):
    minimal_config_dict["processes"][process_name] = {"command": "echo test"}

    with pytest.raises(ImproperlyConfiguredError) as exc_info:
        msgspec.convert(minimal_config_dict, type=Config)

    assert expected_error in exc_info.value.message.lower()


def test_process_config_validates_socket_and_timer_mutual_exclusion():
    with pytest.raises(ImproperlyConfiguredError) as exc_info:
        ProcessConfig(
            command="test",
            socket=True,
            timer=TimerConfig(on_calendar="daily"),
        )

    assert "both" in exc_info.value.message.lower()
    assert "socket" in exc_info.value.message.lower()
    assert "timer" in exc_info.value.message.lower()


@pytest.mark.parametrize(
    "kwargs,expected_error",
    [
        ({"replicas": 2, "socket": True}, "replicas"),
        ({"replicas": 2, "timer": TimerConfig(on_calendar="daily")}, "replicas"),
        ({"replicas": 0}, "at least 1 replica"),
    ],
)
def test_process_config_validates_replicas(kwargs, expected_error):
    with pytest.raises(ImproperlyConfiguredError) as exc_info:
        ProcessConfig(command="test", **kwargs)

    assert expected_error in exc_info.value.message.lower()


def test_timer_config_requires_at_least_one_trigger():
    with pytest.raises(ImproperlyConfiguredError) as exc_info:
        TimerConfig()

    assert "trigger" in exc_info.value.message.lower()


@pytest.mark.parametrize(
    "timer_config",
    [
        TimerConfig(on_calendar="daily"),
        TimerConfig(on_boot_sec="5m"),
        TimerConfig(on_unit_active_sec="1h"),
        TimerConfig(on_active_sec="30m"),
        TimerConfig(on_calendar="hourly", randomized_delay_sec="5m"),
    ],
)
def test_timer_config_accepts_valid_configurations(timer_config):
    assert timer_config is not None


# ============================================================================
# Sites and Routes Validation
# ============================================================================


def test_route_config_requires_exactly_one_type():
    """RouteConfig must specify exactly one of: process or static."""
    # No type specified
    with pytest.raises(ImproperlyConfiguredError) as exc_info:
        msgspec.convert({}, type=RouteConfig)
    assert "exactly one of" in exc_info.value.message.lower()

    # Both types specified
    with pytest.raises(ImproperlyConfiguredError) as exc_info:
        msgspec.convert(
            {"process": "web", "static": "/var/www/static"}, type=RouteConfig
        )
    assert "exactly one of" in exc_info.value.message.lower()


def test_route_config_strip_prefix_only_with_process():
    """strip_prefix only works with process routes."""
    with pytest.raises(ImproperlyConfiguredError) as exc_info:
        msgspec.convert(
            {"static": "/var/www/static", "strip_prefix": "/api"}, type=RouteConfig
        )
    assert "strip_prefix only works with process" in exc_info.value.message.lower()


def test_route_config_process_route():
    """Process route is valid."""
    route = msgspec.convert({"process": "web"}, type=RouteConfig)
    assert route.process == "web"
    assert route.static is None
    assert route.strip_prefix is None


def test_route_config_process_route_with_strip_prefix():
    """Process route with strip_prefix is valid."""
    route = msgspec.convert({"process": "api", "strip_prefix": "/v1"}, type=RouteConfig)
    assert route.process == "api"
    assert route.strip_prefix == "/v1"


def test_route_config_static_route():
    """Static route is valid."""
    route = msgspec.convert({"static": "/var/www/static"}, type=RouteConfig)
    assert route.static == "/var/www/static"
    assert route.process is None


def test_site_config_requires_at_least_one_route():
    """SiteConfig must have at least one route."""
    with pytest.raises(ImproperlyConfiguredError) as exc_info:
        msgspec.convert({"domains": ["example.com"], "routes": {}}, type=SiteConfig)
    assert "at least one route" in exc_info.value.message.lower()


def test_site_config_with_string_routes():
    """SiteConfig accepts string routes (process names)."""
    site = msgspec.convert(
        {"domains": ["example.com"], "routes": {"/": "web"}}, type=SiteConfig
    )
    assert site.domains == ["example.com"]
    assert site.routes["/"] == "web"


def test_site_config_with_dict_routes():
    """SiteConfig accepts dict routes (RouteConfig)."""
    site = msgspec.convert(
        {
            "domains": ["example.com"],
            "routes": {"/api/*": {"process": "api", "strip_prefix": "/api"}},
        },
        type=SiteConfig,
    )
    assert site.domains == ["example.com"]
    assert isinstance(site.routes["/api/*"], RouteConfig)
    assert site.routes["/api/*"].process == "api"
    assert site.routes["/api/*"].strip_prefix == "/api"


def test_site_config_with_mixed_routes():
    """SiteConfig accepts mixed string and dict routes."""
    site = msgspec.convert(
        {
            "domains": ["example.com", "www.example.com"],
            "routes": {
                "/api/*": {"process": "api", "strip_prefix": "/api"},
                "/static/*": {"static": "/var/www/static"},
                "/": "web",
            },
        },
        type=SiteConfig,
    )
    assert len(site.domains) == 2
    assert len(site.routes) == 3
    assert site.routes["/"] == "web"
    assert site.routes["/api/*"].process == "api"
    assert site.routes["/static/*"].static == "/var/www/static"


def test_config_validates_routes_reference_existing_processes(minimal_config_dict):
    """Routes must reference existing processes."""
    minimal_config_dict["processes"] = {"worker": {"command": "celery"}}
    minimal_config_dict["sites"] = [
        {"domains": ["example.com"], "routes": {"/": "web"}}  # web doesn't exist
    ]

    with pytest.raises(ImproperlyConfiguredError) as exc_info:
        msgspec.convert(minimal_config_dict, type=Config)

    assert "unknown process" in exc_info.value.message.lower()


def test_config_validates_routed_processes_have_listen(minimal_config_dict):
    """Routed processes must have listen field."""
    minimal_config_dict["processes"] = {"worker": {"command": "celery"}}  # No listen
    minimal_config_dict["sites"] = [
        {"domains": ["example.com"], "routes": {"/": "worker"}}
    ]

    with pytest.raises(ImproperlyConfiguredError) as exc_info:
        msgspec.convert(minimal_config_dict, type=Config)

    assert "must have 'listen'" in exc_info.value.message.lower()


def test_config_allows_processes_without_listen_when_not_routed(minimal_config_dict):
    """Background processes don't need listen if not routed."""
    minimal_config_dict["processes"] = {
        "web": {"command": "gunicorn", "listen": "localhost:8000"},
        "worker": {"command": "celery", "replicas": 2},  # No listen, not routed
    }
    minimal_config_dict["sites"] = [
        {"domains": ["example.com"], "routes": {"/": "web"}}
    ]

    config = msgspec.convert(minimal_config_dict, type=Config)
    assert "worker" in config.processes
    assert config.processes["worker"].listen is None


def test_config_multiple_sites():
    """Config supports multiple sites."""
    config_dict = {
        "app": "testapp",
        "version": "1.0.0",
        "build_command": "echo building",
        "installation_mode": "python-package",
        "python_version": "3.11",
        "distfile": "dist/testapp-{version}-py3-none-any.whl",
        "processes": {
            "web": {"command": "gunicorn", "listen": "localhost:8000"},
            "api": {"command": "uvicorn", "listen": "localhost:8001"},
        },
        "hosts": [{"address": "example.com", "user": "deploy"}],
        "sites": [
            {"domains": ["example.com"], "routes": {"/": "web"}},
            {"domains": ["api.example.com"], "routes": {"/": "api"}},
        ],
    }

    config = msgspec.convert(config_dict, type=Config)
    assert len(config.sites) == 2
    assert config.sites[0].domains == ["example.com"]
    assert config.sites[1].domains == ["api.example.com"]


def test_config_validates_dict_routes_reference_existing_processes(minimal_config_dict):
    """Dict routes (RouteConfig) must also reference existing processes."""
    minimal_config_dict["processes"] = {
        "web": {"command": "gunicorn", "listen": "localhost:8000"}
    }
    minimal_config_dict["sites"] = [
        {
            "domains": ["example.com"],
            "routes": {
                "/api/*": {"process": "api", "strip_prefix": "/api"}
            },  # api doesn't exist
        }
    ]

    with pytest.raises(ImproperlyConfiguredError) as exc_info:
        msgspec.convert(minimal_config_dict, type=Config)

    assert "unknown process" in exc_info.value.message.lower()


def test_config_validates_dict_routes_have_listen(minimal_config_dict):
    """Dict routes must reference processes with listen."""
    minimal_config_dict["processes"] = {
        "web": {"command": "gunicorn", "listen": "localhost:8000"},
        "api": {"command": "uvicorn"},  # No listen
    }
    minimal_config_dict["sites"] = [
        {
            "domains": ["example.com"],
            "routes": {"/api/*": {"process": "api", "strip_prefix": "/api"}},
        }
    ]

    with pytest.raises(ImproperlyConfiguredError) as exc_info:
        msgspec.convert(minimal_config_dict, type=Config)

    assert "must have 'listen'" in exc_info.value.message.lower()


def test_config_static_routes_dont_require_process(minimal_config_dict):
    """Static routes don't need to reference a process."""
    minimal_config_dict["processes"] = {
        "web": {"command": "gunicorn", "listen": "localhost:8000"}
    }
    minimal_config_dict["sites"] = [
        {
            "domains": ["example.com"],
            "routes": {
                "/static/*": {"static": "/var/www/static"},
                "/": "web",
            },
        }
    ]

    config = msgspec.convert(minimal_config_dict, type=Config)
    assert len(config.sites[0].routes) == 2


# ============================================================================
# Systemd Unit Name Generation
# ============================================================================


def test_unit_template_names(minimal_config_dict):
    minimal_config_dict["processes"]["worker"] = {"command": "celery", "replicas": 3}
    config = msgspec.convert(minimal_config_dict, type=Config)

    # Web process uses app name directly
    assert config.get_unit_template_name("web") == "testapp.service"

    # Non-web process includes process name
    assert config.get_unit_template_name("worker") == "testapp-worker@.service"


def test_unit_names_single_and_multiple_replicas(minimal_config_dict):
    minimal_config_dict["processes"]["worker"] = {"command": "celery", "replicas": 3}
    config = msgspec.convert(minimal_config_dict, type=Config)

    # Single replica returns single unit
    assert config.get_unit_names("web") == ["testapp.service"]

    # Multiple replicas return numbered units
    assert config.get_unit_names("worker") == [
        "testapp-worker@1.service",
        "testapp-worker@2.service",
        "testapp-worker@3.service",
    ]


def test_systemd_units_includes_all_services_sockets_and_timers(minimal_config_dict):
    minimal_config_dict["processes"]["worker"] = {
        "command": "celery",
        "replicas": 2,
    }
    minimal_config_dict["processes"]["scheduled"] = {
        "command": "backup",
        "timer": {"on_calendar": "daily"},
    }
    # Note: socket processes need replicas=1
    minimal_config_dict["processes"]["api"] = {
        "command": "api",
        "socket": True,
    }

    config = msgspec.convert(minimal_config_dict, type=Config)
    units = config.systemd_units

    # Check services
    assert "testapp.service" in units  # web
    assert "testapp-worker@1.service" in units  # worker replica 1
    assert "testapp-worker@2.service" in units  # worker replica 2
    assert "testapp-scheduled.service" in units  # scheduled task
    assert "testapp-api.service" in units  # api with socket

    # Check socket and timer
    assert "testapp.socket" in units  # socket for api process
    assert "testapp-scheduled.timer" in units  # timer for scheduled process


# ============================================================================
# Helper Methods
# ============================================================================


def test_app_bin_returns_correct_path_for_installation_mode(minimal_config_dict):
    # Python package mode uses venv path
    config = msgspec.convert(minimal_config_dict, type=Config)
    assert config.app_bin == ".venv/bin/testapp"

    # Binary mode uses app name directly
    minimal_config_dict["installation_mode"] = "binary"
    config = msgspec.convert(minimal_config_dict, type=Config)
    assert config.app_bin == "testapp"


def test_app_dir_and_release_dir_paths(minimal_config_dict):
    # Default host with relative apps_dir
    config = msgspec.convert(minimal_config_dict, type=Config)
    assert config.app_dir() == "/home/deploy/.local/share/fujin/testapp"
    assert config.get_release_dir() == "/home/deploy/.local/share/fujin/testapp/v1.0.0"
    assert (
        config.get_release_dir("2.0.0")
        == "/home/deploy/.local/share/fujin/testapp/v2.0.0"
    )

    # Multiple hosts with absolute and relative paths
    minimal_config_dict["hosts"] = [
        {
            "name": "prod",
            "address": "prod.com",
            "user": "user1",
            "apps_dir": "/apps",
        },
        {"name": "staging", "address": "staging.com", "user": "user2"},
    ]
    config = msgspec.convert(minimal_config_dict, type=Config)

    # Relative path gets /home/{user} prepended
    staging_host = config.select_host("staging")
    assert config.app_dir(staging_host) == "/home/user2/.local/share/fujin/testapp"

    # Absolute path stays absolute
    prod_host = config.select_host("prod")
    assert config.app_dir(prod_host) == "/apps/testapp"


def test_get_distfile_path(minimal_config_dict):
    config = msgspec.convert(minimal_config_dict, type=Config)

    assert config.get_distfile_path() == Path("dist/testapp-1.0.0-py3-none-any.whl")
    assert config.get_distfile_path("2.0.0") == Path(
        "dist/testapp-2.0.0-py3-none-any.whl"
    )


# ============================================================================
# Template Precedence
# ============================================================================


def test_user_systemd_template_takes_precedence(minimal_config_dict, temp_project_dir):
    config = msgspec.convert(minimal_config_dict, type=Config)

    # Create .fujin directory with custom template
    fujin_dir = temp_project_dir / ".fujin"
    fujin_dir.mkdir()

    custom_template = fujin_dir / "web.service.j2"
    custom_template.write_text(
        "# CUSTOM WEB SERVICE TEMPLATE\n[Service]\nExecStart=custom"
    )

    # Render systemd units
    units, user_templates = config.render_systemd_units()

    # The web service should use the custom template
    assert "testapp.service" in units
    assert "CUSTOM WEB SERVICE TEMPLATE" in units["testapp.service"]
    assert "testapp.service" in user_templates


def test_user_caddyfile_template_takes_precedence(
    minimal_config_dict, temp_project_dir
):
    config = msgspec.convert(minimal_config_dict, type=Config)

    # Create .fujin directory with custom Caddyfile template
    fujin_dir = temp_project_dir / ".fujin"
    fujin_dir.mkdir()

    custom_caddyfile = fujin_dir / "Caddyfile.j2"
    custom_caddyfile.write_text(
        "# CUSTOM CADDY CONFIG\n{{ domain_name }} {\n\tcustom config\n}\n"
    )

    # Render Caddyfile
    caddyfile = config.render_caddyfile()

    # Should use custom template
    assert "CUSTOM CADDY CONFIG" in caddyfile
    assert "custom config" in caddyfile


def test_default_templates_used_when_no_user_templates(
    minimal_config_dict, temp_project_dir
):
    config = msgspec.convert(minimal_config_dict, type=Config)

    # Don't create .fujin directory - use defaults
    units, user_templates = config.render_systemd_units()

    # Should have rendered units but no user templates
    assert "testapp.service" in units
    assert len(user_templates) == 0  # No user templates used

    # Should contain default template markers (adjust based on actual template content)
    assert "[Unit]" in units["testapp.service"]
    assert "[Service]" in units["testapp.service"]


# ============================================================================
# Caddyfile Statics Variable Interpolation
# ============================================================================


def test_caddyfile_statics_interpolates_variables(
    minimal_config_dict, temp_project_dir
):
    """Caddyfile statics values can use variables like {app_dir} and {user}."""
    minimal_config_dict["processes"]["web"]["listen"] = "localhost:8000"
    minimal_config_dict["sites"] = [
        {
            "domains": ["example.com"],
            "routes": {
                "/static/*": {"static": "{app_dir}/static/"},
                "/media/*": {"static": "/var/www/{user}/media/"},
                "/": "web",
            },
        }
    ]

    config = msgspec.convert(minimal_config_dict, type=Config)
    caddyfile = config.render_caddyfile()

    # Variables should be interpolated
    expected_app_dir = config.app_dir(config.hosts[0])
    assert f"{expected_app_dir}/static/" in caddyfile
    assert f"/var/www/{config.hosts[0].user}/media/" in caddyfile
