"""Tests for configuration loading and validation."""

from __future__ import annotations

import msgspec
import pytest

from fujin.config import (
    Config,
    InstallationMode,
    get_git_version,
    read_version_from_pyproject,
)
from fujin.errors import ImproperlyConfiguredError

# Fixtures are imported from conftest.py


# ============================================================================
# Config Loading and Version Handling
# ============================================================================


def test_config_loads_with_explicit_version(minimal_config_dict):
    config = msgspec.convert(minimal_config_dict, type=Config)

    assert config.app_name == "testapp"
    assert config.version == "1.0.0"
    assert config.installation_mode == InstallationMode.PY_PACKAGE


def test_config_version_defaults_to_none_for_git_based(
    minimal_config_dict, temp_project_dir
):
    """When version is not specified, it defaults to None (git-based versioning)."""
    del minimal_config_dict["version"]

    config = msgspec.convert(minimal_config_dict, type=Config)
    # Version field is None, meaning git-based versioning will be used at deploy time
    assert config.version is None


def test_get_deploy_version_uses_explicit_version(minimal_config_dict):
    """get_deploy_version returns explicit version when set."""
    minimal_config_dict["version"] = "1.0.0"
    config = msgspec.convert(minimal_config_dict, type=Config)
    assert config.get_deploy_version() == "1.0.0"


def test_get_deploy_version_uses_git_when_version_not_set(
    minimal_config_dict, temp_project_dir
):
    """get_deploy_version returns git-based version when version is not set."""
    import subprocess

    # Set up a git repo in the temp directory
    subprocess.run(
        ["git", "init"], cwd=temp_project_dir, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=temp_project_dir,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=temp_project_dir,
        check=True,
        capture_output=True,
    )
    # Create an initial commit
    (temp_project_dir / "dummy.txt").write_text("test")
    subprocess.run(
        ["git", "add", "."], cwd=temp_project_dir, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=temp_project_dir,
        check=True,
        capture_output=True,
    )

    del minimal_config_dict["version"]
    config = msgspec.convert(minimal_config_dict, type=Config)

    version = config.get_deploy_version()
    # Git version format: YYYYMMDD-HHMMSS-<hash>[-dirty]
    assert "-" in version
    parts = version.split("-")
    assert len(parts) >= 3  # timestamp date, timestamp time, hash
    assert len(parts[0]) == 8  # YYYYMMDD
    assert len(parts[1]) == 6  # HHMMSS


def test_get_git_version_format():
    """get_git_version returns correctly formatted version string."""
    version = get_git_version()
    # Format: YYYYMMDD-HHMMSS-<hash>[-dirty]
    parts = version.split("-")
    assert len(parts) >= 3

    # Check date part (YYYYMMDD)
    date_part = parts[0]
    assert len(date_part) == 8
    assert date_part.isdigit()

    # Check time part (HHMMSS)
    time_part = parts[1]
    assert len(time_part) == 6
    assert time_part.isdigit()

    # Check hash part (7 chars by default)
    hash_part = parts[2]
    assert len(hash_part) >= 7


@pytest.mark.parametrize(
    "pyproject_content,expected_error",
    [
        (None, "version was not found"),  # No pyproject.toml file
        ("[project]\nname = 'test'", "version was not found"),  # No version key
    ],
)
def test_read_version_from_pyproject_errors(
    tmp_path, monkeypatch, pyproject_content, expected_error
):
    """read_version_from_pyproject raises clear errors for missing file or version."""
    monkeypatch.chdir(tmp_path)
    if pyproject_content:
        (tmp_path / "pyproject.toml").write_text(pyproject_content)

    with pytest.raises(Exception) as exc_info:
        read_version_from_pyproject()

    assert expected_error in str(exc_info.value).lower()


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
    del minimal_config_dict["python_version"]

    config = msgspec.convert(minimal_config_dict, type=Config)
    assert config.installation_mode == InstallationMode.BINARY


# ============================================================================
# Host Configuration
# ============================================================================


@pytest.mark.parametrize(
    "hosts_config,expected_error",
    [
        ([], "at least one host"),
        (
            [
                {"address": "h1.com", "user": "deploy"},
                {"address": "h2.com", "user": "deploy"},
            ],
            "must have a 'name'",
        ),
        (
            [
                {"name": "prod", "address": "h1.com", "user": "deploy"},
                {"name": "prod", "address": "h2.com", "user": "deploy"},
            ],
            "unique",
        ),
    ],
)
def test_config_host_validation_errors(
    minimal_config_dict, hosts_config, expected_error
):
    """Config validates host configuration and raises clear errors."""
    minimal_config_dict["hosts"] = hosts_config

    with pytest.raises(ImproperlyConfiguredError) as exc_info:
        msgspec.convert(minimal_config_dict, type=Config)

    assert expected_error in exc_info.value.message.lower()


def test_config_with_multiple_named_hosts_succeeds(minimal_config_dict):
    minimal_config_dict["hosts"] = [
        {"name": "prod", "address": "prod.com", "user": "deploy"},
        {"name": "staging", "address": "staging.com", "user": "deploy"},
    ]

    config = msgspec.convert(minimal_config_dict, type=Config)
    assert len(config.hosts) == 2


def test_select_host_functionality(minimal_config_dict):
    """select_host returns correct host based on name parameter."""
    # Setup: single host (no name needed)
    minimal_config_dict["hosts"] = [{"address": "example.com", "user": "deploy"}]
    config = msgspec.convert(minimal_config_dict, type=Config)
    assert config.select_host().address == "example.com"

    # Setup: multiple named hosts
    minimal_config_dict["hosts"] = [
        {"name": "prod", "address": "prod.com", "user": "deploy"},
        {"name": "staging", "address": "staging.com", "user": "deploy"},
    ]
    config = msgspec.convert(minimal_config_dict, type=Config)

    # Test selecting by name
    assert config.select_host("staging").address == "staging.com"

    # Test error when name not found
    with pytest.raises(ImproperlyConfiguredError) as exc_info:
        config.select_host("nonexistent")
    assert "not found" in exc_info.value.message.lower()


# ============================================================================
# Distfile and Build Command
# ============================================================================


def test_config_distfile_with_version_placeholder(minimal_config_dict):
    minimal_config_dict["distfile"] = "dist/app-{version}.whl"

    config = msgspec.convert(minimal_config_dict, type=Config)
    assert "{version}" in config.distfile


def test_config_build_command_required(minimal_config_dict):
    del minimal_config_dict["build_command"]

    with pytest.raises(Exception):  # msgspec.ValidationError
        msgspec.convert(minimal_config_dict, type=Config)


# ============================================================================
# Aliases
# ============================================================================


def test_config_aliases_optional(minimal_config_dict):
    # Aliases should default to empty dict if not provided
    config = msgspec.convert(minimal_config_dict, type=Config)
    assert config.aliases == {}


def test_config_with_aliases(minimal_config_dict):
    minimal_config_dict["aliases"] = {
        "shell": "app exec bash",
        "logs": "app logs",
    }

    config = msgspec.convert(minimal_config_dict, type=Config)
    assert config.aliases["shell"] == "app exec bash"
    assert config.aliases["logs"] == "app logs"


# ============================================================================
# Installation Mode
# ============================================================================


def test_config_installation_mode_python_package(minimal_config_dict):
    minimal_config_dict["installation_mode"] = "python-package"

    config = msgspec.convert(minimal_config_dict, type=Config)
    assert config.installation_mode == InstallationMode.PY_PACKAGE


def test_config_installation_mode_binary(minimal_config_dict):
    minimal_config_dict["installation_mode"] = "binary"
    del minimal_config_dict["python_version"]  # Not required for binary

    config = msgspec.convert(minimal_config_dict, type=Config)
    assert config.installation_mode == InstallationMode.BINARY


# ============================================================================
# Replicas Configuration
# ============================================================================


def test_config_replicas_optional(minimal_config_dict):
    # Replicas should default to empty dict if not provided
    config = msgspec.convert(minimal_config_dict, type=Config)
    assert config.replicas == {}


def test_config_with_replicas(minimal_config_dict):
    minimal_config_dict["replicas"] = {
        "web": 3,
        "worker": 5,
    }

    config = msgspec.convert(minimal_config_dict, type=Config)
    assert config.replicas["web"] == 3
    assert config.replicas["worker"] == 5


# ============================================================================
# App Directory and Binary
# ============================================================================


def test_config_app_dir(minimal_config):
    assert minimal_config.app_dir == "/opt/fujin/testapp"


def test_config_app_bin_for_python_package(minimal_config):
    assert minimal_config.installation_mode == InstallationMode.PY_PACKAGE
    assert minimal_config.app_bin == ".install/.venv/bin/testapp"


def test_config_app_bin_for_binary(minimal_config_dict):
    minimal_config_dict["installation_mode"] = "binary"
    del minimal_config_dict["python_version"]

    config = msgspec.convert(minimal_config_dict, type=Config)
    assert config.app_bin == ".install/testapp"
