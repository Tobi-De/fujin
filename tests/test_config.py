"""Tests for configuration loading and validation."""

from __future__ import annotations


import msgspec
import pytest

from fujin.config import (
    Config,
    InstallationMode,
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
    del minimal_config_dict["python_version"]

    config = msgspec.convert(minimal_config_dict, type=Config)
    assert config.installation_mode == InstallationMode.BINARY


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
        {"address": "host1.com", "user": "deploy"},
        {"address": "host2.com", "user": "deploy"},  # Missing name
    ]

    with pytest.raises(ImproperlyConfiguredError) as exc_info:
        msgspec.convert(minimal_config_dict, type=Config)

    assert "must have a 'name'" in exc_info.value.message.lower()


def test_config_with_multiple_named_hosts_succeeds(minimal_config_dict):
    minimal_config_dict["hosts"] = [
        {"name": "prod", "address": "prod.com", "user": "deploy"},
        {"name": "staging", "address": "staging.com", "user": "deploy"},
    ]

    config = msgspec.convert(minimal_config_dict, type=Config)
    assert len(config.hosts) == 2


def test_config_requires_unique_host_names(minimal_config_dict):
    minimal_config_dict["hosts"] = [
        {"name": "prod", "address": "host1.com", "user": "deploy"},
        {"name": "prod", "address": "host2.com", "user": "deploy"},  # Duplicate
    ]

    with pytest.raises(ImproperlyConfiguredError) as exc_info:
        msgspec.convert(minimal_config_dict, type=Config)

    assert "unique" in exc_info.value.message.lower()


def test_select_host_returns_first_when_no_name_specified(minimal_config_dict):
    minimal_config_dict["hosts"] = [{"address": "example.com", "user": "deploy"}]

    config = msgspec.convert(minimal_config_dict, type=Config)
    selected = config.select_host()
    assert selected.address == "example.com"


def test_select_host_by_name(minimal_config_dict):
    minimal_config_dict["hosts"] = [
        {"name": "prod", "address": "prod.com", "user": "deploy"},
        {"name": "staging", "address": "staging.com", "user": "deploy"},
    ]

    config = msgspec.convert(minimal_config_dict, type=Config)
    selected = config.select_host("staging")
    assert selected.address == "staging.com"


def test_select_host_raises_when_name_not_found(minimal_config_dict):
    minimal_config_dict["hosts"] = [
        {"name": "prod", "address": "prod.com", "user": "deploy"}
    ]

    config = msgspec.convert(minimal_config_dict, type=Config)
    with pytest.raises(ImproperlyConfiguredError) as exc_info:
        config.select_host("staging")

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
    assert minimal_config.app_dir() == "/opt/fujin/testapp"


def test_config_app_bin_for_python_package(minimal_config):
    assert minimal_config.installation_mode == InstallationMode.PY_PACKAGE
    assert minimal_config.app_bin == ".venv/bin/testapp"


def test_config_app_bin_for_binary(minimal_config_dict):
    minimal_config_dict["installation_mode"] = "binary"
    del minimal_config_dict["python_version"]

    config = msgspec.convert(minimal_config_dict, type=Config)
    assert config.app_bin == "testapp"
