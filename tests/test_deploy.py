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
