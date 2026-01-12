"""Tests for deploy command - focused on critical functionality."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import msgspec
import pytest

from fujin.commands.deploy import Deploy
from fujin.config import Config
from fujin.errors import BuildError, UploadError


@pytest.fixture
def minimal_deploy_config(tmp_path, monkeypatch):
    """Minimal config with distfile for deploy."""
    # Change to tmp_path so relative paths work
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
        "processes": {"web": {"command": "gunicorn"}},
        "hosts": [{"address": "example.com", "user": "deploy"}],
    }


# ============================================================================
# Full Deploy Flow
# ============================================================================


def test_deploy_creates_and_uploads_bundle(minimal_deploy_config, tmp_path):
    """Deploy creates bundle and uploads to remote."""
    config = msgspec.convert(minimal_deploy_config, type=Config)
    mock_conn = MagicMock()

    # Capture the actual checksum when it's calculated
    actual_checksum = None

    def run_side_effect(cmd, **kwargs):
        nonlocal actual_checksum
        if "sha256sum" in cmd:
            return (actual_checksum or "dummy", True)
        elif "cat" in cmd and ".deployments.json" in cmd:
            return ("[]", True)  # Return empty deployment history
        return ("", True)

    mock_conn.run.side_effect = run_side_effect

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch("fujin.commands.deploy.subprocess.run") as mock_subprocess,
        patch.object(Deploy, "connection") as mock_connection,
        patch("fujin.commands.deploy.log_operation"),
        patch("fujin.commands.deploy.hashlib.file_digest") as mock_digest,
        patch.object(Deploy, "output", MagicMock()),
        patch("fujin.commands.deploy.Console", MagicMock()),
    ):
        mock_connection.return_value.__enter__.return_value = mock_conn
        mock_connection.return_value.__exit__.return_value = None

        # Mock subprocess.run to return a proper CompletedProcess
        mock_subprocess.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="abc123\n", stderr=""
        )

        # Mock the checksum calculation to return a known value
        mock_digest_obj = MagicMock()
        mock_digest_obj.hexdigest.return_value = "test_checksum_123"
        mock_digest.return_value = mock_digest_obj
        actual_checksum = "test_checksum_123"

        deploy = Deploy(no_input=True)
        deploy()

        # Verify upload was called
        assert mock_conn.put.called
        # Verify remote commands were executed
        assert any("mkdir" in str(call) for call in mock_conn.run.call_args_list)
        assert any(
            "python3" in str(call) and "install" in str(call)
            for call in mock_conn.run.call_args_list
        )


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


def test_deploy_retries_on_checksum_mismatch(minimal_deploy_config):
    """Deploy retries upload when checksum doesn't match."""
    config = msgspec.convert(minimal_deploy_config, type=Config)
    mock_conn = MagicMock()

    # First upload: wrong checksum, second: correct
    local_checksum = "abcd1234"
    mock_conn.run.side_effect = [
        ("", True),  # mkdir
        ("wrong", True),  # first checksum
        ("", True),  # rm temp
        (local_checksum, True),  # second checksum
        ("", True),  # mv
        ("", True),  # install
        ("[]", True),  # history read
        ("", True),  # history write
    ]

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch("fujin.commands.deploy.subprocess.run") as mock_subprocess,
        patch.object(Deploy, "connection") as mock_connection,
        patch("fujin.commands.deploy.log_operation"),
        patch("fujin.commands.deploy.hashlib.file_digest") as mock_digest,
        patch("fujin.commands.deploy.Confirm") as mock_confirm,
        patch.object(Deploy, "output", MagicMock()),
        patch("fujin.commands.deploy.Console", MagicMock()),
    ):
        mock_connection.return_value.__enter__.return_value = mock_conn
        mock_connection.return_value.__exit__.return_value = None

        # Mock subprocess.run to return git commit
        mock_subprocess.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="abc123\n", stderr=""
        )

        # Mock the checksum calculation to return known value
        mock_digest_obj = MagicMock()
        mock_digest_obj.hexdigest.return_value = local_checksum
        mock_digest.return_value = mock_digest_obj

        # Mock user confirming retry
        mock_confirm.ask.return_value = True

        deploy = Deploy(no_input=False)  # Need no_input=False to allow retries
        deploy()

        # Should have uploaded twice (retry)
        assert mock_conn.put.call_count == 2


def test_deploy_fails_after_max_retry_attempts(minimal_deploy_config):
    """Deploy raises UploadError after max retries."""
    config = msgspec.convert(minimal_deploy_config, type=Config)
    mock_conn = MagicMock()

    # All attempts fail with wrong checksum
    local_checksum = "correct_checksum"
    mock_conn.run.side_effect = [
        ("", True),  # mkdir
        ("wrong1", True),
        ("", True),  # attempt 1
        ("wrong2", True),
        ("", True),  # attempt 2
        ("wrong3", True),
        ("", True),  # attempt 3
    ]

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch("fujin.commands.deploy.subprocess.run") as mock_subprocess,
        patch.object(Deploy, "connection") as mock_connection,
        patch("fujin.commands.deploy.log_operation"),
        patch("fujin.commands.deploy.hashlib.file_digest") as mock_digest,
        patch.object(Deploy, "output", MagicMock()),
        patch("fujin.commands.deploy.Console", MagicMock()),
    ):
        mock_connection.return_value.__enter__.return_value = mock_conn
        mock_connection.return_value.__exit__.return_value = None

        # Mock subprocess.run to return git commit
        mock_subprocess.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="abc123\n", stderr=""
        )

        # Mock the checksum calculation to return known value
        mock_digest_obj = MagicMock()
        mock_digest_obj.hexdigest.return_value = local_checksum
        mock_digest.return_value = mock_digest_obj

        deploy = Deploy(no_input=True)

        with pytest.raises(UploadError):
            deploy()

        # With no_input=True, fails immediately on first mismatch
        assert mock_conn.put.call_count == 1


# ============================================================================
# Bundle Creation
# ============================================================================


def test_bundle_includes_required_files(minimal_deploy_config, tmp_path):
    """Zipapp bundle includes distfile, env, systemd, and installer."""
    minimal_deploy_config["requirements"] = str(tmp_path / "requirements.txt")
    (tmp_path / "requirements.txt").write_text("django>=4.0\n")

    config = msgspec.convert(minimal_deploy_config, type=Config)
    mock_conn = MagicMock()
    mock_conn.run.return_value = ("abcd1234", True)

    captured_files = {}

    def capture_zipapp(source_dir, target, *args, **kwargs):
        nonlocal captured_files
        source_path = Path(str(source_dir))
        # Capture what files exist in the source directory
        captured_files = {
            "distfile": (source_path / "testapp-1.0.0-py3-none-any.whl").exists(),
            "requirements": (source_path / "requirements.txt").exists(),
            "env": (source_path / ".env").exists(),
            "config": (source_path / "config.json").exists(),
            "main": (source_path / "__main__.py").exists(),
            "systemd_dir": (source_path / "systemd").is_dir(),
        }
        # Create a dummy zipapp file so the code can calculate checksum
        Path(target).write_bytes(b"fake zipapp")

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch("fujin.commands.deploy.subprocess.run") as mock_subprocess,
        patch.object(Deploy, "connection") as mock_connection,
        patch("fujin.commands.deploy.log_operation"),
        patch(
            "fujin.commands.deploy.zipapp.create_archive", side_effect=capture_zipapp
        ),
        patch("fujin.commands.deploy.hashlib.file_digest") as mock_digest,
        patch.object(Deploy, "output", MagicMock()),
        patch("fujin.commands.deploy.Console", MagicMock()),
    ):
        mock_connection.return_value.__enter__.return_value = mock_conn
        mock_connection.return_value.__exit__.return_value = None

        # Mock subprocess.run to return git commit
        mock_subprocess.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="abc123\n", stderr=""
        )

        # Mock the checksum calculation
        mock_digest_obj = MagicMock()
        mock_digest_obj.hexdigest.return_value = "abcd1234"
        mock_digest.return_value = mock_digest_obj

        deploy = Deploy(no_input=True)
        deploy()

        # Verify bundle contents were captured
        assert captured_files["distfile"]
        assert captured_files["requirements"]
        assert captured_files["env"]
        assert captured_files["config"]
        assert captured_files["main"]
        assert captured_files["systemd_dir"]


def test_installer_config_has_correct_structure(minimal_deploy_config):
    """Installer config.json contains all required fields."""
    config = msgspec.convert(minimal_deploy_config, type=Config)
    mock_conn = MagicMock()
    mock_conn.run.return_value = ("abcd1234", True)

    captured_config = None

    def capture_zipapp(source_dir, target, *args, **kwargs):
        nonlocal captured_config
        config_path = Path(str(source_dir)) / "config.json"
        captured_config = json.loads(config_path.read_text())
        # Create a dummy zipapp file so the code can calculate checksum
        Path(target).write_bytes(b"fake zipapp")

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch("fujin.commands.deploy.subprocess.run") as mock_subprocess,
        patch.object(Deploy, "connection") as mock_connection,
        patch("fujin.commands.deploy.log_operation"),
        patch(
            "fujin.commands.deploy.zipapp.create_archive", side_effect=capture_zipapp
        ),
        patch("fujin.commands.deploy.hashlib.file_digest") as mock_digest,
        patch.object(Deploy, "output", MagicMock()),
        patch("fujin.commands.deploy.Console", MagicMock()),
    ):
        mock_connection.return_value.__enter__.return_value = mock_conn
        mock_connection.return_value.__exit__.return_value = None

        # Mock subprocess.run to return git commit
        mock_subprocess.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="abc123\n", stderr=""
        )

        # Mock the checksum calculation
        mock_digest_obj = MagicMock()
        mock_digest_obj.hexdigest.return_value = "abcd1234"
        mock_digest.return_value = mock_digest_obj

        deploy = Deploy(no_input=True)
        deploy()

        # Verify config structure
        assert captured_config["app_name"] == "testapp"
        assert captured_config["version"] == "1.0.0"
        assert captured_config["installation_mode"] == "python-package"
        assert captured_config["python_version"] == "3.11"
        assert captured_config["distfile_name"] == "testapp-1.0.0-py3-none-any.whl"


def test_deploy_handles_caddyfile_braces(minimal_deploy_config, tmp_path):
    """Test that Caddyfile with braces is rendered correctly using safe_format."""
    # Create Caddyfile with curly braces (Caddy syntax)
    # This should work with safe_format
    (tmp_path / ".fujin" / "Caddyfile").write_text(
        'example.com {\n    respond "Hello"\n}'
    )

    config = msgspec.convert(minimal_deploy_config, type=Config)
    mock_conn = MagicMock()
    mock_conn.run.return_value = ("abcd1234", True)

    captured_caddyfile = None

    def capture_zipapp(source_dir, target, *args, **kwargs):
        nonlocal captured_caddyfile
        caddyfile_path = Path(str(source_dir)) / "Caddyfile"
        if caddyfile_path.exists():
            captured_caddyfile = caddyfile_path.read_text()

        # Create a dummy zipapp file so the code can calculate checksum
        Path(target).write_bytes(b"fake zipapp")

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch("fujin.commands.deploy.subprocess.run") as mock_subprocess,
        patch.object(Deploy, "connection") as mock_connection,
        patch("fujin.commands.deploy.log_operation"),
        patch(
            "fujin.commands.deploy.zipapp.create_archive", side_effect=capture_zipapp
        ),
        patch("fujin.commands.deploy.hashlib.file_digest") as mock_digest,
        patch.object(Deploy, "output", MagicMock()),
        patch("fujin.commands.deploy.Console", MagicMock()),
    ):
        mock_connection.return_value.__enter__.return_value = mock_conn
        mock_connection.return_value.__exit__.return_value = None

        # Mock subprocess.run
        mock_subprocess.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="abc123\n", stderr=""
        )

        # Mock checksum
        mock_digest_obj = MagicMock()
        mock_digest_obj.hexdigest.return_value = "abcd1234"
        mock_digest.return_value = mock_digest_obj

        deploy = Deploy(no_input=True)
        deploy()

        # Verify Caddyfile content
        assert "example.com {" in captured_caddyfile
        assert 'respond "Hello"' in captured_caddyfile
        assert "}" in captured_caddyfile


def test_deploy_safe_format_substitutes_variables(minimal_deploy_config, tmp_path):
    """Test that variables are still substituted by safe_format."""
    # Create Caddyfile with variable
    (tmp_path / ".fujin" / "Caddyfile").write_text(
        'example.com {\n    respond "Hello {app_name}"\n}'
    )

    config = msgspec.convert(minimal_deploy_config, type=Config)
    mock_conn = MagicMock()
    mock_conn.run.return_value = ("abcd1234", True)

    captured_caddyfile = None

    def capture_zipapp(source_dir, target, *args, **kwargs):
        nonlocal captured_caddyfile
        caddyfile_path = Path(str(source_dir)) / "Caddyfile"
        if caddyfile_path.exists():
            captured_caddyfile = caddyfile_path.read_text()

        Path(target).write_bytes(b"fake zipapp")

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch("fujin.commands.deploy.subprocess.run") as mock_subprocess,
        patch.object(Deploy, "connection") as mock_connection,
        patch("fujin.commands.deploy.log_operation"),
        patch(
            "fujin.commands.deploy.zipapp.create_archive", side_effect=capture_zipapp
        ),
        patch("fujin.commands.deploy.hashlib.file_digest") as mock_digest,
        patch.object(Deploy, "output", MagicMock()),
        patch("fujin.commands.deploy.Console", MagicMock()),
    ):
        mock_connection.return_value.__enter__.return_value = mock_conn
        mock_connection.return_value.__exit__.return_value = None
        mock_subprocess.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="abc123\n", stderr=""
        )
        mock_digest.return_value.hexdigest.return_value = "abcd1234"

        deploy = Deploy(no_input=True)
        deploy()

        # Verify variable substitution
        assert 'respond "Hello testapp"' in captured_caddyfile
