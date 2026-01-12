"""Shared fixtures for all tests."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import msgspec
import pytest

from fujin.commands import BaseCommand
from fujin.config import Config


@pytest.fixture
def minimal_config_dict(tmp_path, monkeypatch):
    """Minimal valid configuration dict for file-based structure."""
    monkeypatch.chdir(tmp_path)

    return {
        "app": "testapp",
        "version": "1.0.0",
        "build_command": "echo building",
        "installation_mode": "python-package",
        "python_version": "3.11",
        "distfile": "dist/testapp-{version}-py3-none-any.whl",
        "hosts": [{"address": "example.com", "user": "deploy"}],
    }


@pytest.fixture
def minimal_config(minimal_config_dict):
    """Convert minimal_config_dict to Config object."""
    return msgspec.convert(minimal_config_dict, type=Config)


@pytest.fixture
def temp_project_dir(tmp_path, monkeypatch):
    """Temporary project directory with pyproject.toml."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    pyproject = project_dir / "pyproject.toml"
    pyproject.write_text("""
[project]
name = "testapp"
version = "2.5.0"
""")

    monkeypatch.chdir(project_dir)
    return project_dir


@pytest.fixture
def mock_connection():
    """Mocked SSH connection for command tests."""
    mock_conn = MagicMock()
    mock_conn.run.return_value = ("", True)

    with patch.object(BaseCommand, "connection") as mock_connection_ctx:
        mock_connection_ctx.return_value.__enter__.return_value = mock_conn
        mock_connection_ctx.return_value.__exit__.return_value = None
        yield mock_conn


@pytest.fixture
def mock_output():
    """Mocked output handler for command tests."""
    with patch.object(BaseCommand, "output", MagicMock()) as mock_out:
        yield mock_out
