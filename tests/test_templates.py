"""Tests for templates command."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import msgspec
import pytest

from fujin.commands.templates import Templates
from fujin.config import Config


@pytest.fixture
def templates_config(minimal_config_dict):
    """Config with multiple process types for testing template selection."""
    minimal_config_dict["processes"] = {
        "web": {"command": "gunicorn app:app", "socket": True},
        "worker": {"command": "celery worker", "replicas": 2},
        "health": {"command": "health-check", "timer": {"on_calendar": "*:*:00"}},
    }
    return minimal_config_dict


# ============================================================================
# Eject All Templates
# ============================================================================


def test_eject_all_templates_when_no_name_provided(templates_config, tmp_path):
    """Ejecting with no name copies all default templates."""
    config = msgspec.convert(templates_config, type=Config)
    local_config_dir = tmp_path / ".fujin"

    # Create mock package templates directory with all template files
    package_templates = tmp_path / "package_templates"
    package_templates.mkdir()
    template_files = [
        "default.service.j2",
        "web.service.j2",
        "default.socket.j2",
        "default.timer.j2",
        "Caddyfile.j2",
    ]
    for template_file in template_files:
        (package_templates / template_file).write_text(f"# {template_file}")

    # Update config to use our test directories
    config.local_config_dir = local_config_dir

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch(
            "fujin.config.Config._package_templates_path",
            return_value=package_templates,
        ),
        patch.object(Templates, "output", MagicMock()) as mock_output,
    ):
        templates = Templates()
        templates.eject(name=None)

        # Verify .fujin directory was created
        assert local_config_dir.exists()

        # Verify all templates were copied
        for template_file in template_files:
            target_file = local_config_dir / template_file
            assert target_file.exists()
            assert target_file.read_text() == f"# {template_file}"

        # Verify success message
        assert mock_output.success.called
        success_message = mock_output.success.call_args[0][0]
        assert "Ejected 5 template(s)" in success_message


def test_eject_all_skips_non_j2_files(templates_config, tmp_path):
    """Eject all only copies .j2 files, ignoring other files."""
    config = msgspec.convert(templates_config, type=Config)
    local_config_dir = tmp_path / ".fujin"

    package_templates = tmp_path / "package_templates"
    package_templates.mkdir()

    # Create mix of .j2 and non-.j2 files
    (package_templates / "default.service.j2").write_text("# service")
    (package_templates / "README.md").write_text("# readme")
    (package_templates / "__pycache__").mkdir()

    config.local_config_dir = local_config_dir

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch(
            "fujin.config.Config._package_templates_path",
            return_value=package_templates,
        ),
        patch.object(Templates, "output", MagicMock()),
    ):
        templates = Templates()
        templates.eject(name=None)

        # Only .j2 file should be ejected
        assert (local_config_dir / "default.service.j2").exists()
        assert not (local_config_dir / "README.md").exists()


# ============================================================================
# Eject Specific Process Templates
# ============================================================================


def test_eject_web_process_uses_web_service_template(templates_config, tmp_path):
    """Ejecting web process uses web.service.j2 instead of default.service.j2."""
    config = msgspec.convert(templates_config, type=Config)
    local_config_dir = tmp_path / ".fujin"

    package_templates = tmp_path / "package_templates"
    package_templates.mkdir()
    (package_templates / "web.service.j2").write_text("# web service")
    (package_templates / "default.socket.j2").write_text("# socket")

    config.local_config_dir = local_config_dir

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch(
            "fujin.config.Config._package_templates_path",
            return_value=package_templates,
        ),
        patch.object(Templates, "output", MagicMock()) as mock_output,
    ):
        templates = Templates()
        templates.eject(name="web")

        # Should eject web.service.j2 (not default.service.j2) and default.socket.j2
        assert (local_config_dir / "web.service.j2").exists()
        assert not (local_config_dir / "default.service.j2").exists()
        assert (local_config_dir / "default.socket.j2").exists()

        # Verify success message shows 2 templates
        assert mock_output.success.called
        success_message = mock_output.success.call_args[0][0]
        assert "Ejected 2 template(s)" in success_message


def test_eject_worker_process_uses_default_service(templates_config, tmp_path):
    """Ejecting non-web process uses default.service.j2."""
    config = msgspec.convert(templates_config, type=Config)
    local_config_dir = tmp_path / ".fujin"

    package_templates = tmp_path / "package_templates"
    package_templates.mkdir()
    (package_templates / "default.service.j2").write_text("# default service")

    config.local_config_dir = local_config_dir

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch(
            "fujin.config.Config._package_templates_path",
            return_value=package_templates,
        ),
        patch.object(Templates, "output", MagicMock()),
    ):
        templates = Templates()
        templates.eject(name="worker")

        # Should eject default.service.j2
        assert (local_config_dir / "default.service.j2").exists()


def test_eject_process_with_timer_includes_timer_template(templates_config, tmp_path):
    """Ejecting process with timer includes default.timer.j2."""
    config = msgspec.convert(templates_config, type=Config)
    local_config_dir = tmp_path / ".fujin"

    package_templates = tmp_path / "package_templates"
    package_templates.mkdir()
    (package_templates / "default.service.j2").write_text("# service")
    (package_templates / "default.timer.j2").write_text("# timer")

    config.local_config_dir = local_config_dir

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch(
            "fujin.config.Config._package_templates_path",
            return_value=package_templates,
        ),
        patch.object(Templates, "output", MagicMock()) as mock_output,
    ):
        templates = Templates()
        templates.eject(name="health")

        # Should eject both service and timer templates
        assert (local_config_dir / "default.service.j2").exists()
        assert (local_config_dir / "default.timer.j2").exists()

        # Verify 2 templates were ejected
        success_message = mock_output.success.call_args[0][0]
        assert "Ejected 2 template(s)" in success_message


# ============================================================================
# Eject Caddy Template
# ============================================================================


def test_eject_caddy_template(templates_config, tmp_path):
    """Ejecting caddy copies Caddyfile.j2."""
    config = msgspec.convert(templates_config, type=Config)
    local_config_dir = tmp_path / ".fujin"

    package_templates = tmp_path / "package_templates"
    package_templates.mkdir()
    (package_templates / "Caddyfile.j2").write_text("# caddyfile")

    config.local_config_dir = local_config_dir

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch(
            "fujin.config.Config._package_templates_path",
            return_value=package_templates,
        ),
        patch.object(Templates, "output", MagicMock()) as mock_output,
    ):
        templates = Templates()
        templates.eject(name="caddy")

        # Should eject Caddyfile.j2
        assert (local_config_dir / "Caddyfile.j2").exists()
        assert (local_config_dir / "Caddyfile.j2").read_text() == "# caddyfile"

        # Verify success message
        success_message = mock_output.success.call_args[0][0]
        assert "Ejected 1 template(s)" in success_message


# ============================================================================
# Error Handling
# ============================================================================


def test_eject_error_handling(templates_config, tmp_path):
    """Test error handling for invalid process names and missing templates."""
    config = msgspec.convert(templates_config, type=Config)
    local_config_dir = tmp_path / ".fujin"

    package_templates = tmp_path / "package_templates"
    package_templates.mkdir()

    config.local_config_dir = local_config_dir

    # Test 1: Invalid process name exits with error
    with (
        patch("fujin.config.Config.read", return_value=config),
        patch(
            "fujin.config.Config._package_templates_path",
            return_value=package_templates,
        ),
        pytest.raises(SystemExit) as exc_info,
    ):
        templates = Templates()
        templates.eject(name="invalid_process")

    assert exc_info.value.code == 1

    # Test 2: Missing template file shows warning
    with (
        patch("fujin.config.Config.read", return_value=config),
        patch(
            "fujin.config.Config._package_templates_path",
            return_value=package_templates,
        ),
        patch.object(Templates, "output", MagicMock()) as mock_output,
    ):
        templates = Templates()
        templates.eject(name="web")

        # Should show warning about missing template
        assert mock_output.warning.called


# ============================================================================
# Overwrite Behavior
# ============================================================================


def test_eject_existing_file_overwrite_behavior(templates_config, tmp_path):
    """Ejecting existing file prompts for overwrite - skips on decline, overwrites on confirm."""
    config = msgspec.convert(templates_config, type=Config)
    local_config_dir = tmp_path / ".fujin"
    local_config_dir.mkdir()

    existing_file = local_config_dir / "default.service.j2"
    existing_file.write_text("# existing content")

    package_templates = tmp_path / "package_templates"
    package_templates.mkdir()
    (package_templates / "default.service.j2").write_text("# new content")

    config.local_config_dir = local_config_dir

    # Test 1: User declines overwrite
    with (
        patch("fujin.config.Config.read", return_value=config),
        patch(
            "fujin.config.Config._package_templates_path",
            return_value=package_templates,
        ),
        patch.object(Templates, "output", MagicMock()) as mock_output,
        patch("fujin.commands.templates.Confirm.ask", return_value=False),
    ):
        templates = Templates()
        templates.eject(name="worker")

        # File should remain unchanged
        assert existing_file.read_text() == "# existing content"
        assert mock_output.warning.called
        warning_message = mock_output.warning.call_args[0][0]
        assert "Skipped 1 existing template(s)" in warning_message

    # Test 2: User confirms overwrite
    with (
        patch("fujin.config.Config.read", return_value=config),
        patch(
            "fujin.config.Config._package_templates_path",
            return_value=package_templates,
        ),
        patch.object(Templates, "output", MagicMock()) as mock_output,
        patch("fujin.commands.templates.Confirm.ask", return_value=True),
    ):
        templates = Templates()
        templates.eject(name="worker")

        # File should be overwritten
        assert existing_file.read_text() == "# new content"
        assert mock_output.success.called
