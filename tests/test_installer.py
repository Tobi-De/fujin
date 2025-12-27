"""Tests for zipapp installer."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fujin._installer.__main__ import InstallConfig, install, uninstall


@pytest.fixture
def bundle_dir(tmp_path):
    """Create a fake bundle directory with all necessary files."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()

    # Create .env file
    (bundle / ".env").write_text("DATABASE_URL=postgres://localhost/db\n")

    # Create units directory with fake systemd units
    units_dir = bundle / "units"
    units_dir.mkdir()
    (units_dir / "testapp.service").write_text("[Unit]\nDescription=Test App\n")
    (units_dir / "testapp-worker.service").write_text(
        "[Unit]\nDescription=Test Worker\n"
    )

    # Create fake distfile
    (bundle / "testapp-1.0.0-py3-none-any.whl").write_text("fake wheel content")

    # Create fake requirements.txt
    (bundle / "requirements.txt").write_text("django>=4.0\n")

    # Create Caddyfile
    (bundle / "Caddyfile").write_text(
        "example.com {\n\treverse_proxy localhost:8000\n}\n"
    )

    return bundle


@pytest.fixture
def python_package_config(tmp_path):
    """Config for python-package installation mode."""
    app_dir = tmp_path / "app_install"
    return InstallConfig(
        app_name="testapp",
        app_dir=str(app_dir),
        version="1.0.0",
        installation_mode="python-package",
        python_version="3.11",
        requirements=True,
        distfile_name="testapp-1.0.0-py3-none-any.whl",
        release_command=None,
        webserver_enabled=False,
        caddy_config_path="/etc/caddy/conf.d/testapp.caddy",
        app_bin=".venv/bin/testapp",
        active_units=["testapp.service", "testapp-worker.service"],
        valid_units=["testapp.service", "testapp-worker.service"],
        user_units=[],
    )


@pytest.fixture
def binary_config(tmp_path):
    """Config for binary installation mode."""
    app_dir = tmp_path / "app_install"
    return InstallConfig(
        app_name="testapp",
        app_dir=str(app_dir),
        version="1.0.0",
        installation_mode="binary",
        python_version=None,
        requirements=False,
        distfile_name="testapp-1.0.0-linux-x86_64",
        release_command=None,
        webserver_enabled=False,
        caddy_config_path="/etc/caddy/conf.d/testapp.caddy",
        app_bin="testapp",
        active_units=["testapp.service"],
        valid_units=["testapp.service"],
        user_units=[],
    )


# ============================================================================
# Install - Python Package Mode
# ============================================================================


def test_install_python_package(bundle_dir, python_package_config):
    """Install creates directories, files, and runs uv commands for python package."""
    with (
        patch("fujin._installer.__main__.run") as mock_run,
        patch("fujin._installer.__main__.log"),
    ):
        app_dir = Path(python_package_config.app_dir)
        distfile_path = bundle_dir / python_package_config.distfile_name
        requirements_path = bundle_dir / "requirements.txt"

        install(python_package_config, bundle_dir)

        # Assert file system state
        assert app_dir.exists()
        assert (app_dir / ".env").exists()
        assert (
            app_dir / ".env"
        ).read_text() == "DATABASE_URL=postgres://localhost/db\n"
        assert (app_dir / ".appenv").exists()
        appenv = (app_dir / ".appenv").read_text()
        assert "UV_PYTHON=python3.11" in appenv
        assert 'PATH=".venv/bin:$PATH"' in appenv
        assert (app_dir / ".version").exists()
        assert (app_dir / ".version").read_text() == "1.0.0"

        # Verify exact commands were called
        calls = [call[0][0] for call in mock_run.call_args_list]
        assert f"uv python install {python_package_config.python_version}" in calls
        assert "test -d .venv || uv venv" in calls
        assert (
            f"uv pip install -r {requirements_path} && uv pip install --no-deps {distfile_path}"
            in calls
        )


def test_install_with_release_command_executes_it(bundle_dir, python_package_config):
    """Install executes release command when specified."""
    python_package_config.release_command = "python manage.py migrate"

    with (
        patch("fujin._installer.__main__.run") as mock_run,
        patch("fujin._installer.__main__.log"),
    ):
        install(python_package_config, bundle_dir)

        # Verify release command was called (wrapped in bash -lc with .appenv)
        calls = [call[0][0] for call in mock_run.call_args_list]
        release_cmd = next((c for c in calls if "python manage.py migrate" in c), None)
        assert release_cmd is not None
        assert "bash -lc" in release_cmd
        assert "source .appenv" in release_cmd
        assert "python manage.py migrate" in release_cmd


# ============================================================================
# Install - Binary Mode
# ============================================================================


def test_install_binary(bundle_dir, binary_config):
    """Binary install copies binary, sets permissions, and creates .appenv."""
    # Create fake binary in bundle
    (bundle_dir / "testapp-1.0.0-linux-x86_64").write_bytes(b"\x7fELF fake binary")

    with (
        patch("fujin._installer.__main__.run"),
        patch("fujin._installer.__main__.log"),
    ):
        app_dir = Path(binary_config.app_dir)

        install(binary_config, bundle_dir)

        # Assert binary was copied and is executable
        binary_path = app_dir / "testapp"
        assert binary_path.exists()
        assert binary_path.read_bytes() == b"\x7fELF fake binary"
        assert binary_path.stat().st_mode & 0o111  # Check executable bits

        # Assert .appenv has correct PATH
        appenv_content = (app_dir / ".appenv").read_text()
        assert f'export PATH="{app_dir}:$PATH"' in appenv_content


# ============================================================================
# Install - Systemd Unit Handling
# ============================================================================


def test_install_removes_stale_units(bundle_dir, python_package_config):
    """Install stops and removes stale systemd units not in valid_units."""

    def run_side_effect(cmd, **kwargs):
        # Return stale units from systemctl list command
        if "list-unit-files" in cmd:
            mock_result = MagicMock()
            mock_result.stdout = (
                "testapp.service\ntestapp-worker.service\ntestapp-old.service\n"
            )
            mock_result.returncode = 0
            return mock_result
        return MagicMock(returncode=0)

    with (
        patch("fujin._installer.__main__.run", side_effect=run_side_effect) as mock_run,
        patch("fujin._installer.__main__.log"),
    ):
        install(python_package_config, bundle_dir)

        # Verify stale unit was stopped and disabled
        calls = [call[0][0] for call in mock_run.call_args_list]
        all_cmds = " ".join(calls)
        assert "systemctl stop testapp-old.service" in all_cmds
        assert "systemctl disable testapp-old.service" in all_cmds


# ============================================================================
# Install - Webserver Configuration
# ============================================================================


def test_install_with_webserver_configures_caddy(bundle_dir, python_package_config):
    """Install configures Caddy when webserver is enabled."""
    python_package_config.webserver_enabled = True

    def run_side_effect(cmd, **kwargs):
        # Make caddy validation succeed
        if "caddy validate" in cmd:
            mock_result = MagicMock()
            mock_result.returncode = 0
            return mock_result
        return MagicMock(returncode=0)

    with (
        patch("fujin._installer.__main__.run", side_effect=run_side_effect) as mock_run,
        patch("fujin._installer.__main__.log"),
    ):
        install(python_package_config, bundle_dir)

        # Verify all caddy setup commands appear (may be combined)
        calls = [call[0][0] for call in mock_run.call_args_list]
        all_cmds = " ".join(calls)
        assert "mkdir -p /etc/caddy/conf.d" in all_cmds
        assert "caddy validate --config Caddyfile" in all_cmds
        assert f"mv Caddyfile {python_package_config.caddy_config_path}" in all_cmds
        assert "systemctl reload caddy" in all_cmds


# ============================================================================
# Uninstall
# ============================================================================


def test_uninstall_stops_and_disables_services(python_package_config):
    """Uninstall stops and disables all services."""
    with (
        patch("fujin._installer.__main__.run") as mock_run,
        patch("fujin._installer.__main__.log"),
    ):
        uninstall(python_package_config)

        # Verify services are disabled and stopped
        calls = [call[0][0] for call in mock_run.call_args_list]
        disable_cmd = next((c for c in calls if "systemctl disable --now" in c), None)
        assert disable_cmd is not None
        assert "testapp.service" in disable_cmd
        assert "testapp-worker.service" in disable_cmd


def test_uninstall_removes_unit_files(python_package_config):
    """Uninstall removes systemd unit files."""
    with (
        patch("fujin._installer.__main__.run") as mock_run,
        patch("fujin._installer.__main__.log"),
    ):
        uninstall(python_package_config)

        # Verify exact rm commands for unit files
        calls = [call[0][0] for call in mock_run.call_args_list]
        assert "sudo rm -f /etc/systemd/system/testapp.service" in calls
        assert "sudo rm -f /etc/systemd/system/testapp-worker.service" in calls


def test_uninstall_with_webserver_removes_caddy_config(python_package_config):
    """Uninstall removes Caddy configuration when webserver was enabled."""
    python_package_config.webserver_enabled = True

    with (
        patch("fujin._installer.__main__.run") as mock_run,
        patch("fujin._installer.__main__.log"),
    ):
        uninstall(python_package_config)

        # Verify Caddy config removal and reload (may be combined)
        calls = [call[0][0] for call in mock_run.call_args_list]
        all_cmds = " ".join(calls)
        assert f"rm -f {python_package_config.caddy_config_path}" in all_cmds
        assert "systemctl reload caddy" in all_cmds
