"""Tests for scale command."""

from __future__ import annotations


import pytest

from fujin.commands.scale import Scale
from fujin.config import tomllib


@pytest.fixture
def scale_command(mock_output):
    """Fixture to create Scale command instance with mocked output."""
    return Scale(service="worker", count=1)


def test_scale_zero_raises_error(mock_output):
    """Scaling to 0 raises error."""
    scale = Scale(service="worker", count=0)

    with pytest.raises(SystemExit) as exc:
        scale()

    assert exc.value.code == 1
    mock_output.error.assert_called()
    assert "Cannot scale to 0" in mock_output.error.call_args[0][0]


def test_scale_negative_raises_error(mock_output):
    """Scaling to negative number raises error."""
    scale = Scale(service="worker", count=-1)

    with pytest.raises(SystemExit) as exc:
        scale()

    assert exc.value.code == 1
    mock_output.error.assert_called_with("Replica count must be 1 or greater")


def test_scale_no_systemd_dir_error(tmp_path, monkeypatch, mock_output):
    """Error if .fujin/systemd directory doesn't exist."""
    monkeypatch.chdir(tmp_path)
    scale = Scale(service="worker", count=2)

    with pytest.raises(SystemExit) as exc:
        scale()

    assert exc.value.code == 1
    mock_output.error.assert_called()
    assert "not found" in mock_output.error.call_args[0][0]


def test_scale_service_not_found_error(tmp_path, monkeypatch, mock_output):
    """Error if service file doesn't exist."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".fujin/systemd").mkdir(parents=True)

    scale = Scale(service="worker", count=2)

    with pytest.raises(SystemExit) as exc:
        scale()

    assert exc.value.code == 1
    mock_output.error.assert_called()
    assert "not found" in mock_output.error.call_args[0][0]


def test_scale_to_one_converts_template(tmp_path, monkeypatch, mock_output):
    """Scaling template service to 1 converts it to regular service."""
    monkeypatch.chdir(tmp_path)
    systemd_dir = tmp_path / ".fujin/systemd"
    systemd_dir.mkdir(parents=True)

    # Create template service
    template_file = systemd_dir / "worker@.service"
    template_file.write_text("Description=Worker %i")

    # Create fujin.toml with existing config
    fujin_toml = tmp_path / "fujin.toml"
    fujin_toml.write_text('app = "test"\n[replicas]\nworker = 3')

    scale = Scale(service="worker", count=1)
    scale()

    # Check conversion
    assert not template_file.exists()
    regular_file = systemd_dir / "worker.service"
    assert regular_file.exists()
    assert regular_file.read_text() == "Description=Worker "

    # Check fujin.toml update
    content = fujin_toml.read_text()
    config = tomllib.loads(content)
    assert "replicas" not in config  # Should be removed since it was the only one


def test_scale_to_multiple_converts_regular(tmp_path, monkeypatch, mock_output):
    """Scaling regular service to >1 converts it to template service."""
    monkeypatch.chdir(tmp_path)
    systemd_dir = tmp_path / ".fujin/systemd"
    systemd_dir.mkdir(parents=True)

    # Create regular service
    regular_file = systemd_dir / "worker.service"
    regular_file.write_text("Description={{app_name}} worker")

    # Create fujin.toml
    fujin_toml = tmp_path / "fujin.toml"
    fujin_toml.write_text('app = "test"')

    scale = Scale(service="worker", count=3)
    scale()

    # Check conversion
    assert not regular_file.exists()
    template_file = systemd_dir / "worker@.service"
    assert template_file.exists()
    assert template_file.read_text() == "Description={{app_name}} worker %i"

    # Check fujin.toml update
    content = fujin_toml.read_text()
    config = tomllib.loads(content)
    assert config["replicas"]["worker"] == 3


def test_scale_socket_warning(tmp_path, monkeypatch, mock_output):
    """Warning shown when scaling socket-activated service."""
    monkeypatch.chdir(tmp_path)
    systemd_dir = tmp_path / ".fujin/systemd"
    systemd_dir.mkdir(parents=True)

    (systemd_dir / "web.service").write_text("content")
    (systemd_dir / "web.socket").touch()

    scale = Scale(service="web", count=2)
    scale()

    mock_output.warning.assert_called()
    assert (
        "Scaling a socket-activated service is not recommended"
        in mock_output.warning.call_args[0][0]
    )
