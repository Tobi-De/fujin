"""Tests for deploy command - error handling and edge cases.

Full deployment workflows are tested in integration tests.
See tests/integration/test_full_deploy.py
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import msgspec
import pytest

from fujin.commands.deploy import Deploy
from fujin.config import Config
from fujin.discovery import DeployedUnit
from fujin.errors import BuildError


@pytest.fixture
def minimal_deploy_config(tmp_path, monkeypatch):
    """Minimal config with distfile for deploy."""
    monkeypatch.chdir(tmp_path)

    # Create distfile
    dist_dir = Path("dist")
    dist_dir.mkdir()
    (dist_dir / "testapp-1.0.0-py3-none-any.whl").write_text("fake wheel")

    # Create .fujin/systemd directory with a sample service
    fujin_systemd = Path(".fujin/systemd")
    fujin_systemd.mkdir(parents=True)
    (fujin_systemd / "web.service").write_text(
        "[Unit]\nDescription={app_name}\n[Service]\nExecStart=/bin/true\n"
    )

    return {
        "app": "testapp",
        "version": "1.0.0",
        "build_command": "echo building",
        "installation_mode": "python-package",
        "python_version": "3.11",
        "distfile": "dist/testapp-{version}-py3-none-any.whl",
        "hosts": [{"address": "example.com", "user": "deploy"}],
    }


# ============================================================================
# Error Scenarios
# ============================================================================


def test_deploy_fails_when_build_command_fails(minimal_deploy_config):
    """Deploy raises BuildError when build command fails."""
    config = msgspec.convert(minimal_deploy_config, type=Config)

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch(
            "fujin.commands.deploy.subprocess.run",
            side_effect=subprocess.CalledProcessError(1, "echo building"),
        ),
        patch.object(Deploy, "output", MagicMock()),
        patch("fujin.commands.deploy.Console", MagicMock()),
    ):
        deploy = Deploy(no_input=True)

        with pytest.raises(BuildError):
            deploy()


def test_deploy_fails_when_requirements_missing(minimal_deploy_config, tmp_path):
    """Deploy raises BuildError when requirements file specified but missing."""
    minimal_deploy_config["requirements"] = str(tmp_path / "missing.txt")
    config = msgspec.convert(minimal_deploy_config, type=Config)

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch("fujin.commands.deploy.subprocess.run") as mock_subprocess,
        patch.object(Deploy, "output", MagicMock()),
        patch("fujin.commands.deploy.Console", MagicMock()),
    ):
        # Mock subprocess.run to return success for build command
        mock_subprocess.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )

        deploy = Deploy(no_input=True)

        with pytest.raises(BuildError):
            deploy()


# ============================================================================
# Service Context Variables
# ============================================================================


@pytest.fixture
def service_context_project(tmp_path, monkeypatch):
    """Project with multiple services for testing context variable generation."""
    monkeypatch.chdir(tmp_path)

    fujin_systemd = Path(".fujin/systemd")
    fujin_systemd.mkdir(parents=True)

    # Service with socket (web)
    (fujin_systemd / "web.service").write_text(
        "[Unit]\nDescription=Web\n[Service]\nExecStart=/bin/true\n"
    )
    (fujin_systemd / "web.socket").write_text(
        "[Unit]\nDescription=Web Socket\n[Socket]\nListenStream=/run/app.sock\n"
    )

    # Service with timer (scheduler)
    (fujin_systemd / "scheduler.service").write_text(
        "[Unit]\nDescription=Scheduler\n[Service]\nExecStart=/bin/true\n"
    )
    (fujin_systemd / "scheduler.timer").write_text(
        "[Unit]\nDescription=Scheduler Timer\n[Timer]\nOnCalendar=hourly\n"
    )

    # Standalone service without socket or timer (worker)
    (fujin_systemd / "worker.service").write_text(
        "[Unit]\nDescription=Worker\n[Service]\nExecStart=/bin/true\n"
    )

    return tmp_path


def _build_context_for_units(deployed_units: list[DeployedUnit]) -> dict[str, str]:
    """Build the service context dict matching deploy.py logic."""
    context = {}
    for du in deployed_units:
        if du.socket_file:
            context[f"{du.name}_socket"] = du.template_socket_name
        if du.timer_file:
            context[f"{du.name}_timer"] = du.template_timer_name
        if not du.socket_file and not du.timer_file and not du.is_template:
            context[f"{du.name}_service"] = du.template_service_name
    return context


def test_service_with_socket_exposes_socket_variable(service_context_project):
    """Service with socket exposes {name}_socket, not {name}_service."""
    du = DeployedUnit(
        name="web",
        app_name="myapp",
        service_file=Path(".fujin/systemd/web.service"),
        socket_file=Path(".fujin/systemd/web.socket"),
    )

    context = _build_context_for_units([du])

    assert "web_socket" in context
    assert context["web_socket"] == "myapp-web.socket"
    assert "web_service" not in context


def test_service_with_timer_exposes_timer_variable(service_context_project):
    """Service with timer exposes {name}_timer, not {name}_service."""
    du = DeployedUnit(
        name="scheduler",
        app_name="myapp",
        service_file=Path(".fujin/systemd/scheduler.service"),
        timer_file=Path(".fujin/systemd/scheduler.timer"),
    )

    context = _build_context_for_units([du])

    assert "scheduler_timer" in context
    assert context["scheduler_timer"] == "myapp-scheduler.timer"
    assert "scheduler_service" not in context


def test_standalone_service_exposes_service_variable(service_context_project):
    """Service without socket or timer exposes {name}_service."""
    du = DeployedUnit(
        name="worker",
        app_name="myapp",
        service_file=Path(".fujin/systemd/worker.service"),
    )

    context = _build_context_for_units([du])

    assert "worker_service" in context
    assert context["worker_service"] == "myapp-worker.service"
    assert "worker_socket" not in context
    assert "worker_timer" not in context


def test_service_with_both_socket_and_timer_exposes_both(service_context_project):
    """Service with both socket and timer exposes both, but not service."""
    du = DeployedUnit(
        name="api",
        app_name="myapp",
        service_file=Path(".fujin/systemd/api.service"),
        socket_file=Path(".fujin/systemd/api.socket"),
        timer_file=Path(".fujin/systemd/api.timer"),
    )

    context = _build_context_for_units([du])

    assert "api_socket" in context
    assert "api_timer" in context
    assert "api_service" not in context
    assert context["api_socket"] == "myapp-api.socket"
    assert context["api_timer"] == "myapp-api.timer"


def test_multiple_services_build_complete_context(service_context_project):
    """Multiple services with different configurations build correct context."""
    units = [
        DeployedUnit(
            name="web",
            app_name="myapp",
            service_file=Path(".fujin/systemd/web.service"),
            socket_file=Path(".fujin/systemd/web.socket"),
        ),
        DeployedUnit(
            name="scheduler",
            app_name="myapp",
            service_file=Path(".fujin/systemd/scheduler.service"),
            timer_file=Path(".fujin/systemd/scheduler.timer"),
        ),
        DeployedUnit(
            name="worker",
            app_name="myapp",
            service_file=Path(".fujin/systemd/worker.service"),
        ),
    ]

    context = _build_context_for_units(units)

    # web has socket
    assert context["web_socket"] == "myapp-web.socket"
    assert "web_service" not in context

    # scheduler has timer
    assert context["scheduler_timer"] == "myapp-scheduler.timer"
    assert "scheduler_service" not in context

    # worker is standalone
    assert context["worker_service"] == "myapp-worker.service"


def test_template_service_without_socket_or_timer_not_exposed():
    """Template service (replicas > 1) without socket/timer is not exposed."""
    du = DeployedUnit(
        name="worker",
        app_name="myapp",
        service_file=Path(".fujin/systemd/worker@.service"),
        replicas=3,
    )

    context = _build_context_for_units([du])

    # Template services without socket/timer are not useful as dependency targets
    assert "worker_service" not in context
    assert "worker_socket" not in context
    assert "worker_timer" not in context
