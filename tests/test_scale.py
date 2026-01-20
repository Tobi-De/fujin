"""Tests for scale command."""

from __future__ import annotations


import pytest

from fujin.commands.app import App
from fujin.config import tomllib


def test_scale_zero_raises_error(mock_output):
    """Scaling to 0 raises error."""
    app = App()

    with pytest.raises(SystemExit) as exc:
        app.scale(service="worker", count=0)

    assert exc.value.code == 1
    mock_output.error.assert_called()
    assert "Cannot scale to 0" in mock_output.error.call_args[0][0]


def test_scale_negative_raises_error(mock_output):
    """Scaling to negative number raises error."""
    app = App()

    with pytest.raises(SystemExit) as exc:
        app.scale(service="worker", count=-1)

    assert exc.value.code == 1
    mock_output.error.assert_called_with("Replica count must be 1 or greater")


def test_scale_no_systemd_dir_error(tmp_path, monkeypatch, mock_output):
    """Error if .fujin/systemd directory doesn't exist."""
    monkeypatch.chdir(tmp_path)

    # Create fujin.toml (needed for config loading)
    fujin_toml = tmp_path / "fujin.toml"
    fujin_toml.write_text("""
app = "testapp"
version = "1.0.0"
build_command = "echo building"
installation_mode = "python-package"
python_version = "3.11"
distfile = "dist/testapp-{version}.whl"

[[hosts]]
address = "example.com"
user = "deploy"
""")

    app = App()

    with pytest.raises(SystemExit) as exc:
        app.scale(service="worker", count=2)

    assert exc.value.code == 1
    mock_output.error.assert_called()
    assert "not found" in mock_output.error.call_args[0][0]


def test_scale_service_not_found_error(tmp_path, monkeypatch, mock_output):
    """Error if service file doesn't exist."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".fujin/systemd").mkdir(parents=True)

    # Create fujin.toml so config can be loaded
    fujin_toml = tmp_path / "fujin.toml"
    fujin_toml.write_text("""
app = "testapp"
version = "1.0.0"
build_command = "echo building"
installation_mode = "python-package"
python_version = "3.11"
distfile = "dist/testapp-{version}.whl"

[[hosts]]
address = "example.com"
user = "deploy"
""")

    app = App()

    with pytest.raises(SystemExit) as exc:
        app.scale(service="worker", count=2)

    assert exc.value.code == 1
    mock_output.error.assert_called()
    assert "not found" in mock_output.error.call_args[0][0]


def test_scale_to_one_converts_template(
    tmp_path, monkeypatch, mock_output, mock_connection
):
    """Scaling template service to 1 converts it to regular service."""
    monkeypatch.chdir(tmp_path)
    systemd_dir = tmp_path / ".fujin/systemd"
    systemd_dir.mkdir(parents=True)

    # Create template service (valid INI format)
    template_file = systemd_dir / "worker@.service"
    template_file.write_text("[Unit]\nDescription=Worker %i\n")

    # Create fujin.toml with existing config
    fujin_toml = tmp_path / "fujin.toml"
    fujin_toml.write_text("""
app = "test"
version = "1.0.0"
build_command = "echo building"
installation_mode = "python-package"
python_version = "3.11"
distfile = "dist/test-{version}.whl"

[[hosts]]
address = "example.com"
user = "deploy"

[replicas]
worker = 3
""")

    app = App()
    app.scale(service="worker", count=1)

    # Check conversion
    assert not template_file.exists()
    regular_file = systemd_dir / "worker.service"
    assert regular_file.exists()
    assert regular_file.read_text() == "[Unit]\nDescription=Worker \n"

    # Check fujin.toml update
    content = fujin_toml.read_text()
    config = tomllib.loads(content)
    assert "replicas" not in config  # Should be removed since it was the only one

    # Check that we did NOT call the server (conversion from template to regular requires deploy)
    mock_connection.run.assert_not_called()
    # Should show deploy message
    mock_output.info.assert_called()
    info_message = mock_output.info.call_args[0][0]
    assert "fujin deploy" in info_message


def test_scale_to_multiple_converts_regular(
    tmp_path, monkeypatch, mock_output, mock_connection
):
    """Scaling regular service to >1 converts it to template service."""
    monkeypatch.chdir(tmp_path)
    systemd_dir = tmp_path / ".fujin/systemd"
    systemd_dir.mkdir(parents=True)

    # Create regular service (valid INI format)
    regular_file = systemd_dir / "worker.service"
    regular_file.write_text("[Unit]\nDescription={{app_name}} worker\n")

    # Create fujin.toml
    fujin_toml = tmp_path / "fujin.toml"
    fujin_toml.write_text("""
app = "test"
version = "1.0.0"
build_command = "echo building"
installation_mode = "python-package"
python_version = "3.11"
distfile = "dist/test-{version}.whl"

[[hosts]]
address = "example.com"
user = "deploy"
""")

    app = App()
    app.scale(service="worker", count=3)

    # Check conversion
    assert not regular_file.exists()
    template_file = systemd_dir / "worker@.service"
    assert template_file.exists()
    assert template_file.read_text() == "[Unit]\nDescription={{app_name}} worker %i\n"

    # Check fujin.toml update
    content = fujin_toml.read_text()
    config = tomllib.loads(content)
    assert config["replicas"]["worker"] == 3

    # Check that we did NOT call the server (conversion requires deploy)
    mock_connection.run.assert_not_called()
    # Should show deploy message
    mock_output.info.assert_called()
    info_message = mock_output.info.call_args[0][0]
    assert "fujin deploy" in info_message


def test_scale_socket_warning(tmp_path, monkeypatch, mock_output, mock_connection):
    """Warning shown when scaling socket-activated service."""
    monkeypatch.chdir(tmp_path)
    systemd_dir = tmp_path / ".fujin/systemd"
    systemd_dir.mkdir(parents=True)

    # Create valid systemd unit files
    (systemd_dir / "web.service").write_text("[Unit]\nDescription=Web\n")
    (systemd_dir / "web.socket").write_text("[Socket]\nListenStream=8000\n")

    # Create fujin.toml
    fujin_toml = tmp_path / "fujin.toml"
    fujin_toml.write_text("""
app = "test"
version = "1.0.0"
build_command = "echo building"
installation_mode = "python-package"
python_version = "3.11"
distfile = "dist/test-{version}.whl"

[[hosts]]
address = "example.com"
user = "deploy"
""")

    app = App()
    app.scale(service="web", count=2)

    mock_output.warning.assert_called()
    assert (
        "Scaling a socket-activated service is not recommended"
        in mock_output.warning.call_args[0][0]
    )


def test_scale_template_up_calls_server(
    tmp_path, monkeypatch, mock_output, mock_connection
):
    """Scaling up a template service calls systemctl on server."""
    monkeypatch.chdir(tmp_path)
    systemd_dir = tmp_path / ".fujin/systemd"
    systemd_dir.mkdir(parents=True)

    # Create template service (already a template)
    template_file = systemd_dir / "worker@.service"
    template_file.write_text("[Unit]\nDescription=Worker %i\n")

    # Create fujin.toml with current replica count of 2
    fujin_toml = tmp_path / "fujin.toml"
    fujin_toml.write_text("""
app = "test"
version = "1.0.0"
build_command = "echo building"
installation_mode = "python-package"
python_version = "3.11"
distfile = "dist/test-{version}.whl"

[[hosts]]
address = "example.com"
user = "deploy"

[replicas]
worker = 2
""")

    # Scale from 2 to 4
    app = App()
    app.scale(service="worker", count=4)

    # Should have called server to start instances 3 and 4
    mock_connection.run.assert_called()
    call_args = mock_connection.run.call_args[0][0]
    assert "test-worker@3.service" in call_args
    assert "test-worker@4.service" in call_args
    assert "systemctl start" in call_args

    # Check fujin.toml was updated
    content = fujin_toml.read_text()
    config = tomllib.loads(content)
    assert config["replicas"]["worker"] == 4


def test_scale_template_down_calls_server(
    tmp_path, monkeypatch, mock_output, mock_connection
):
    """Scaling down a template service calls systemctl on server."""
    monkeypatch.chdir(tmp_path)
    systemd_dir = tmp_path / ".fujin/systemd"
    systemd_dir.mkdir(parents=True)

    # Create template service (already a template)
    template_file = systemd_dir / "worker@.service"
    template_file.write_text("[Unit]\nDescription=Worker %i\n")

    # Create fujin.toml with current replica count of 5
    fujin_toml = tmp_path / "fujin.toml"
    fujin_toml.write_text("""
app = "test"
version = "1.0.0"
build_command = "echo building"
installation_mode = "python-package"
python_version = "3.11"
distfile = "dist/test-{version}.whl"

[[hosts]]
address = "example.com"
user = "deploy"

[replicas]
worker = 5
""")

    # Scale from 5 to 2
    app = App()
    app.scale(service="worker", count=2)

    # Should have called server to stop instances 3, 4, and 5
    mock_connection.run.assert_called()
    call_args = mock_connection.run.call_args[0][0]
    assert "test-worker@3.service" in call_args
    assert "test-worker@4.service" in call_args
    assert "test-worker@5.service" in call_args
    assert "systemctl stop" in call_args

    # Check fujin.toml was updated
    content = fujin_toml.read_text()
    config = tomllib.loads(content)
    assert config["replicas"]["worker"] == 2
