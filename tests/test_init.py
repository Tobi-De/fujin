"""Tests for init command."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from fujin.commands.init import Init
from fujin.config import tomllib


# clean_dir fixture is now handled via tmp_path and monkeypatch from conftest


# ============================================================================
# Basic Config Generation
# ============================================================================


def test_init_creates_fujin_toml_with_simple_profile(tmp_path, monkeypatch):
    """init creates fujin.toml with simple profile by default."""
    monkeypatch.chdir(tmp_path)
    clean_dir = tmp_path
    with patch.object(Init, "output", MagicMock()) as mock_output:
        init = Init()
        init()

        # Verify fujin.toml was created
        assert (clean_dir / "fujin.toml").exists()

        # Verify config content (minimal, no processes/sites)
        config = tomllib.loads((clean_dir / "fujin.toml").read_text())
        assert config["app"] == clean_dir.name
        assert config["installation_mode"] == "python-package"
        assert "processes" not in config
        assert "sites" not in config

        # Verify .fujin/ directory structure
        assert (clean_dir / ".fujin").exists()
        assert (clean_dir / ".fujin/systemd").exists()
        assert (clean_dir / ".fujin/systemd/web.service").exists()
        assert (clean_dir / ".fujin/systemd/web.socket").exists()
        assert (clean_dir / ".fujin/systemd/common.d").exists()
        assert (clean_dir / ".fujin/systemd/common.d/base.conf").exists()
        assert (clean_dir / ".fujin/Caddyfile").exists()

        # Verify success messages (includes file creation messages)
        mock_output.success.assert_any_call("Generated fujin.toml")
        mock_output.success.assert_any_call(
            "Generated .fujin/ directory with simple profile"
        )


@pytest.mark.parametrize("profile", ["django", "falco", "binary"])
def test_init_with_profiles(tmp_path, monkeypatch, profile):
    """init creates valid config for all profiles."""
    monkeypatch.chdir(tmp_path)

    with patch.object(Init, "output", MagicMock()):
        init = Init(profile=profile)
        init()

        # Verify fujin.toml was created with minimal structure
        assert (tmp_path / "fujin.toml").exists()
        config = tomllib.loads((tmp_path / "fujin.toml").read_text())
        assert "app" in config
        assert "hosts" in config
        assert "processes" not in config
        assert "sites" not in config

        # Verify .fujin/ directory structure
        assert (tmp_path / ".fujin").exists()
        assert (tmp_path / ".fujin/systemd").exists()
        assert (tmp_path / ".fujin/Caddyfile").exists()

        # Profile-specific checks
        if profile == "django":
            # Django has web service with migrations
            web_service = (tmp_path / ".fujin/systemd/web.service").read_text()
            assert "migrate" in web_service
            assert "collectstatic" in web_service
        elif profile == "falco":
            # Falco has web + worker
            assert (tmp_path / ".fujin/systemd/web.service").exists()
            assert (tmp_path / ".fujin/systemd/worker.service").exists()
        elif profile == "binary":
            # Binary has web service without .venv
            web_service = (tmp_path / ".fujin/systemd/web.service").read_text()
            assert ".venv" not in web_service


# ============================================================================
# Existing File Handling
# ============================================================================


def test_init_skips_when_fujin_toml_exists(tmp_path, monkeypatch):
    """init skips and shows warning when fujin.toml already exists."""
    monkeypatch.chdir(tmp_path)
    # Create existing file
    (tmp_path / "fujin.toml").write_text("existing content")

    with patch.object(Init, "output", MagicMock()) as mock_output:
        init = Init()
        init()

        # Should not overwrite
        assert (tmp_path / "fujin.toml").read_text() == "existing content"

        # Should show warning
        mock_output.warning.assert_called_with(
            "fujin.toml file already exists, skipping generation"
        )


def test_init_skips_when_fujin_dir_exists(tmp_path, monkeypatch):
    """init skips and shows warning when .fujin/ directory already exists."""
    monkeypatch.chdir(tmp_path)
    # Create existing directory
    (tmp_path / ".fujin").mkdir()
    (tmp_path / ".fujin/test.txt").write_text("existing content")

    with patch.object(Init, "output", MagicMock()) as mock_output:
        init = Init()
        init()

        # Should not create fujin.toml or modify .fujin/
        assert not (tmp_path / "fujin.toml").exists()
        assert (tmp_path / ".fujin/test.txt").read_text() == "existing content"

        # Should show warning
        mock_output.warning.assert_called_with(
            ".fujin/ directory already exists, skipping generation"
        )


# ============================================================================
# pyproject.toml Integration
# ============================================================================


def test_init_reads_app_name_from_pyproject(tmp_path, monkeypatch):
    """init reads app name from pyproject.toml if it exists."""
    monkeypatch.chdir(tmp_path)
    pyproject_content = """
[project]
name = "my-awesome-app"
version = "1.2.3"
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)

    with patch.object(Init, "output", MagicMock()):
        init = Init()
        init()

        config = tomllib.loads((tmp_path / "fujin.toml").read_text())
        assert config["app"] == "my-awesome-app"


def test_init_omits_version_when_in_pyproject(tmp_path, monkeypatch):
    """init omits version field when it exists in pyproject.toml."""
    monkeypatch.chdir(tmp_path)
    pyproject_content = """
[project]
name = "myapp"
version = "1.2.3"
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)

    with patch.object(Init, "output", MagicMock()):
        init = Init()
        init()

        config = tomllib.loads((tmp_path / "fujin.toml").read_text())
        # Version should not be in config (fujin will read from pyproject)
        assert "version" not in config


def test_init_keeps_version_when_not_in_pyproject(tmp_path, monkeypatch):
    """init keeps version field when not in pyproject.toml."""
    monkeypatch.chdir(tmp_path)
    pyproject_content = """
[project]
name = "myapp"
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)

    with patch.object(Init, "output", MagicMock()):
        init = Init()
        init()

        config = tomllib.loads((tmp_path / "fujin.toml").read_text())
        # Version should be in config
        assert config["version"] == "0.0.1"


# ============================================================================
# Python Version Handling
# ============================================================================


def test_init_adds_python_version_when_no_python_version_file(tmp_path, monkeypatch):
    """init adds python_version to config when .python-version doesn't exist."""
    monkeypatch.chdir(tmp_path)
    with patch.object(Init, "output", MagicMock()):
        init = Init()
        init()

        config = tomllib.loads((tmp_path / "fujin.toml").read_text())
        assert config["python_version"] == "3.12"


def test_init_omits_python_version_when_python_version_file_exists(
    tmp_path, monkeypatch
):
    """init omits python_version when .python-version file exists."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".python-version").write_text("3.11.5")

    with patch.object(Init, "output", MagicMock()):
        init = Init()
        init()

        config = tomllib.loads((tmp_path / "fujin.toml").read_text())
        # python_version should NOT be in config (fujin will read from .python-version)
        assert "python_version" not in config


# ============================================================================
# App Name Derivation
# ============================================================================


def test_init_derives_app_name_from_directory_name(tmp_path, monkeypatch):
    """init derives app name from current directory name."""
    app_dir = tmp_path / "my-test-app"
    app_dir.mkdir()
    monkeypatch.chdir(app_dir)

    with patch.object(Init, "output", MagicMock()):
        init = Init()
        init()

        config = tomllib.loads((app_dir / "fujin.toml").read_text())
        # Should normalize hyphens and spaces to underscores
        assert config["app"] == "my_test_app"


def test_init_normalizes_app_name_with_spaces(tmp_path, monkeypatch):
    """init normalizes app name by replacing spaces with underscores."""
    app_dir = tmp_path / "my test app"
    app_dir.mkdir()
    monkeypatch.chdir(app_dir)

    with patch.object(Init, "output", MagicMock()):
        init = Init()
        init()

        config = tomllib.loads((app_dir / "fujin.toml").read_text())
        assert config["app"] == "my_test_app"


# ============================================================================
# Config Structure Validation
# ============================================================================


def test_init_config_has_required_fields(tmp_path, monkeypatch):
    """init creates config with all required fields."""
    monkeypatch.chdir(tmp_path)
    with patch.object(Init, "output", MagicMock()):
        init = Init()
        init()

        config = tomllib.loads((tmp_path / "fujin.toml").read_text())

        # Required top-level fields (minimal config, no processes/sites)
        assert "app" in config
        assert "build_command" in config
        assert "distfile" in config
        assert "installation_mode" in config
        assert "hosts" in config

        # These should NOT be in the minimal config
        assert "processes" not in config
        assert "sites" not in config

        # Verify .fujin/ directory has the actual service/site definitions
        assert (tmp_path / ".fujin/systemd").exists()
        assert (tmp_path / ".fujin/Caddyfile").exists()


def test_init_config_has_aliases(tmp_path, monkeypatch):
    """init creates config with command aliases."""
    monkeypatch.chdir(tmp_path)
    with patch.object(Init, "output", MagicMock()):
        init = Init()
        init()

        config = tomllib.loads((tmp_path / "fujin.toml").read_text())
        assert "aliases" in config
        assert isinstance(config["aliases"], dict)
        assert len(config["aliases"]) > 0
