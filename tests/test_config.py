import pytest
from pathlib import Path
from unittest.mock import patch
from fujin.config import Config, ProcessConfig, Webserver, HostConfig, InstallationMode
from fujin.errors import ImproperlyConfiguredError


def test_process_config_validation_socket_and_timer():
    with pytest.raises(ImproperlyConfiguredError):
        ProcessConfig(command="cmd", socket=True, timer="OnCalendar=daily")


def test_config_validation_missing_web_process():
    with pytest.raises(ImproperlyConfiguredError):
        Config(
            app_name="testapp",
            build_command="build",
            distfile="dist.whl",
            installation_mode=InstallationMode.BINARY,
            host=HostConfig(domain_name="example.com", user="user"),
            webserver=Webserver(upstream="localhost:8000", enabled=True),
            processes={"worker": ProcessConfig(command="work")},
        )


def test_config_no_processes():
    with pytest.raises(ImproperlyConfiguredError):
        Config(
            app_name="testapp",
            build_command="build",
            distfile="dist.whl",
            installation_mode=InstallationMode.BINARY,
            host=HostConfig(domain_name="example.com", user="user"),
            webserver=Webserver(upstream="localhost:8000", enabled=False),
            processes={},
        )


def test_host_config_validation_env_and_envfile():
    with pytest.raises(ImproperlyConfiguredError):
        HostConfig(
            domain_name="example.com",
            user="user",
            env_content="FOO=bar",
            _env_file=".env",
        )


def test_host_config_envfile_loading():
    with (
        patch("pathlib.Path.exists", return_value=True),
        patch("pathlib.Path.read_text", return_value="FOO=bar"),
    ):
        host = HostConfig(
            domain_name="example.com",
            user="user",
            _env_file=".env",
        )
        assert host.env_content == "FOO=bar"


def test_service_name_resolution(mock_config):
    assert mock_config.get_unit_template_name("web") == "testapp.service"
    assert mock_config.get_unit_template_name("worker") == "testapp-worker@.service"

    mock_config.processes["beat"] = ProcessConfig(command="beat")
    assert mock_config.get_unit_template_name("beat") == "testapp-beat.service"


def test_get_active_unit_names(mock_config):
    assert mock_config.get_active_unit_names("web") == ["testapp.service"]
    assert mock_config.get_active_unit_names("worker") == [
        "testapp-worker@1.service",
        "testapp-worker@2.service",
    ]


def test_active_systemd_units_property(mock_config):
    mock_config.processes["cleanup"] = ProcessConfig(
        command="cleanup", timer="OnCalendar=daily"
    )
    mock_config.processes["api"] = ProcessConfig(command="api", socket=True)

    active_systemd_units = mock_config.active_systemd_units

    assert "testapp.service" in active_systemd_units
    assert "testapp-worker@1.service" in active_systemd_units
    assert "testapp-worker@2.service" in active_systemd_units
    assert "testapp-cleanup.timer" in active_systemd_units
    assert "testapp.socket" in active_systemd_units


def test_render_systemd_units_filenames(mock_config):
    units, _ = mock_config.render_systemd_units()
    assert "testapp.service" in units
    assert "testapp-worker@.service" in units


def test_render_systemd_units_with_timer_filenames(mock_config):
    mock_config.processes["cleanup"] = ProcessConfig(
        command="cleanup", timer="OnCalendar=daily"
    )
    units, _ = mock_config.render_systemd_units()
    assert "testapp-cleanup.timer" in units
    assert "testapp-cleanup.service" in units


def test_render_systemd_units_with_socket_filenames(mock_config):
    mock_config.processes["api"] = ProcessConfig(command="api", socket=True)
    units, _ = mock_config.render_systemd_units()
    assert "testapp.socket" in units
    assert "testapp-api.service" in units


def test_app_bin_property(mock_config):
    mock_config.installation_mode = InstallationMode.PY_PACKAGE
    assert mock_config.app_bin == ".venv/bin/testapp"

    mock_config.installation_mode = InstallationMode.BINARY
    assert mock_config.app_bin == "testapp"


def test_get_distfile_path(mock_config):
    mock_config.distfile = "dist/app-{version}.whl"
    mock_config.version = "1.0.0"
    assert mock_config.get_distfile_path() == Path("dist/app-1.0.0.whl")
    assert mock_config.get_distfile_path("2.0.0") == Path("dist/app-2.0.0.whl")


def test_render_caddyfile_with_statics(mock_config):
    mock_config.webserver.statics = {"/static/*": "/var/www/static/"}
    caddyfile = mock_config.render_caddyfile()

    assert "handle_path /static/* {" in caddyfile
    assert "root * /var/www/static/" in caddyfile
    assert "file_server" in caddyfile
    assert "reverse_proxy localhost:8000" in caddyfile


def test_process_config_validation_replicas_and_socket_or_timer():
    with pytest.raises(ImproperlyConfiguredError):
        ProcessConfig(command="cmd", replicas=2, socket=True)

    with pytest.raises(ImproperlyConfiguredError):
        ProcessConfig(command="cmd", replicas=2, timer="OnCalendar=daily")


def test_caddy_config_path(mock_config):
    assert mock_config.caddy_config_path == "/etc/caddy/conf.d/testapp.caddy"

    mock_config.webserver.config_dir = "/custom/path"
    assert mock_config.caddy_config_path == "/custom/path/testapp.caddy"
