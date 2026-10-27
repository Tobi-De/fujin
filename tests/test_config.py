"""Tests for configuration loading and validation."""

from __future__ import annotations

from io import StringIO

import msgspec
import pytest
from dotenv import dotenv_values

from fujin.config import (
    Config,
    InstallationMode,
    merge_env_templates,
    read_version_from_pyproject,
)
from fujin.errors import ImproperlyConfiguredError
from fujin.secrets import render_template

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
    assert minimal_config.app_bin == "current/venv/bin/testapp"


def test_config_app_bin_for_binary(minimal_config_dict):
    minimal_config_dict["installation_mode"] = "binary"
    del minimal_config_dict["python_version"]

    config = msgspec.convert(minimal_config_dict, type=Config)
    assert config.app_bin == "current/bin/testapp"


# ============================================================================
# App-level env template + host overlay (#84)
# ============================================================================


def test_project_env_merges_host_overlay_in_place(minimal_config_dict):
    minimal_config_dict["env"] = "A=1\nB=2"
    minimal_config_dict["hosts"][0]["env"] = "B=3\nC=4"

    config = msgspec.convert(minimal_config_dict, type=Config)
    assert config.effective_env(config.hosts[0]) == "A=1\nB=3\nC=4"


def test_host_overlay_replaces_last_project_declaration():
    """dotenv is last-wins, so the LAST project line for a key is replaced."""
    assert merge_env_templates("A=1\nA=2\nB=9", "A=3") == "A=1\nA=3\nB=9"


def test_merge_without_project_env_is_host_env():
    host_env = "# host only\nA=1\nB=$REF\n"
    assert merge_env_templates("", host_env) == "# host only\nA=1\nB=$REF"
    assert merge_env_templates("  ", host_env) == "# host only\nA=1\nB=$REF"


def test_merge_without_host_env_is_project_env():
    project = "# project\nA=1\nB=$$LITERAL"
    assert merge_env_templates(project, "") == project
    assert merge_env_templates(project, "   \n") == project


def test_merge_ignores_host_comments_and_blanks():
    merged = merge_env_templates("A=1", "# a comment\n\nB=2\n# trailing")
    assert merged == "A=1\nB=2"


def test_merge_preserves_template_text_verbatim():
    """$REF, $$ escapes and {placeholders} survive the merge untouched."""
    project = "TOKEN=$SECRET\nESCAPED=$$NOT_A_REF\nURL={app_name}.example"
    merged = merge_env_templates(project, "EXTRA=$OTHER_REF")
    assert merged == f"{project}\nEXTRA=$OTHER_REF"


def test_config_env_and_envfile_are_mutually_exclusive(minimal_config_dict, tmp_path):
    envfile = tmp_path / ".env.base"
    envfile.write_text("A=1\n")
    minimal_config_dict["env"] = "A=1"
    minimal_config_dict["envfile"] = str(envfile)

    with pytest.raises(ImproperlyConfiguredError) as exc_info:
        msgspec.convert(minimal_config_dict, type=Config)
    assert "both 'env' and 'envfile'" in exc_info.value.message


def test_config_reads_env_from_envfile(minimal_config_dict, tmp_path):
    envfile = tmp_path / ".env.base"
    envfile.write_text("  A=1\nB=2  \n")
    minimal_config_dict["envfile"] = str(envfile)

    config = msgspec.convert(minimal_config_dict, type=Config)
    assert config.env_content == "A=1\nB=2"


def test_config_envfile_missing_errors(minimal_config_dict, tmp_path):
    minimal_config_dict["envfile"] = str(tmp_path / "nope.env")

    with pytest.raises(ImproperlyConfiguredError) as exc_info:
        msgspec.convert(minimal_config_dict, type=Config)
    assert "not found" in exc_info.value.message


def test_host_only_secret_ref_resolves_through_adapter(
    minimal_config_dict, monkeypatch
):
    """A $REF declared only in the host overlay reaches the secrets pipeline."""
    from fujin.secrets import resolve_secrets

    minimal_config_dict["env"] = "A=1"
    minimal_config_dict["hosts"][0]["env"] = "HOST_SECRET=$HOST_SECRET_ENV"
    config = msgspec.convert(minimal_config_dict, type=Config)
    monkeypatch.setenv("HOST_SECRET_ENV", "resolved-value")

    rendered = resolve_secrets(
        config.effective_env(config.hosts[0]), config.secret_config
    )

    assert "HOST_SECRET=resolved-value" in rendered
    assert "A=1" in rendered


# ============================================================================
# Block-based, quote-aware env merge (#84)
# ============================================================================


def test_host_multiline_quoted_value_survives_merge():
    host = 'CERT="line1\nline2"'
    merged = merge_env_templates("B=2", host)

    assert merged == 'B=2\nCERT="line1\nline2"'
    with StringIO(merged) as buffer:
        assert dotenv_values(stream=buffer) == {"B": "2", "CERT": "line1\nline2"}


def test_project_multiline_value_not_corrupted_by_other_key_override():
    project = 'CERT="a\nb=1\nc"\nPORT=8000'
    merged = merge_env_templates(project, "PORT=9000")

    assert merged == 'CERT="a\nb=1\nc"\nPORT=9000'
    assert "b=1" in merged  # the quoted continuation is still inside CERT


def test_project_multiline_value_override_drops_orphan_continuation():
    project = 'CERT="a\nb"\nPORT=8000'
    merged = merge_env_templates(project, 'CERT="new"')

    assert merged == 'CERT="new"\nPORT=8000'
    assert "a\nb" not in merged


def test_export_with_tab_overrides_project_key():
    merged = merge_env_templates("A=1\nB=2", "export\tA=9")

    assert merged == "export\tA=9\nB=2"


def test_bare_key_host_line_is_kept_and_rendered_as_empty():
    merged = merge_env_templates("A=1", "EMPTY")

    assert merged == "A=1\nEMPTY"
    assert render_template(merged) == "A=1\nEMPTY="


def test_empty_value_override():
    merged = merge_env_templates("KEY=old\nB=2", "KEY=")

    assert merged == "KEY=\nB=2"


def test_override_value_containing_equals():
    merged = merge_env_templates("URL=http://old\nB=2", "URL=a=b")

    assert merged == "URL=a=b\nB=2"
    with StringIO(merged) as buffer:
        assert dotenv_values(stream=buffer)["URL"] == "a=b"
