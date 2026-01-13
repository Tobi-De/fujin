"""Tests for new command."""

from __future__ import annotations


import pytest

from fujin.commands.new import New


@pytest.fixture
def new_command(mock_output):
    """Fixture to create New command instance with mocked output."""
    return New()


def test_new_service_creates_file(tmp_path, monkeypatch, new_command):
    """new service creates a service file."""
    monkeypatch.chdir(tmp_path)

    new_command.service(name="worker")

    service_file = tmp_path / ".fujin/systemd/worker.service"
    assert service_file.exists()
    content = service_file.read_text()
    assert "[Unit]" in content
    assert "Description={app_name} worker" in content
    assert "[Service]" in content


def test_new_service_exists_error(tmp_path, monkeypatch, new_command):
    """new service errors if file already exists."""
    monkeypatch.chdir(tmp_path)

    # Create file first
    systemd_dir = tmp_path / ".fujin/systemd"
    systemd_dir.mkdir(parents=True)
    (systemd_dir / "worker.service").touch()

    with pytest.raises(SystemExit) as exc:
        new_command.service(name="worker")

    assert exc.value.code == 1
    new_command.output.error.assert_called_with(
        f".fujin/systemd/worker.service already exists"
    )


def test_new_timer_creates_files(tmp_path, monkeypatch, new_command):
    """new timer creates service and timer files."""
    monkeypatch.chdir(tmp_path)

    new_command.timer(name="cleanup")

    service_file = tmp_path / ".fujin/systemd/cleanup.service"
    timer_file = tmp_path / ".fujin/systemd/cleanup.timer"

    assert service_file.exists()
    assert timer_file.exists()

    service_content = service_file.read_text()
    assert "Type=oneshot" in service_content

    timer_content = timer_file.read_text()
    assert "[Timer]" in timer_content
    assert "OnCalendar=daily" in timer_content


def test_new_timer_exists_error(tmp_path, monkeypatch, new_command):
    """new timer errors if files already exist."""
    monkeypatch.chdir(tmp_path)

    # Create file first
    systemd_dir = tmp_path / ".fujin/systemd"
    systemd_dir.mkdir(parents=True)
    (systemd_dir / "backup.timer").touch()

    with pytest.raises(SystemExit) as exc:
        new_command.timer(name="backup")

    assert exc.value.code == 1
    new_command.output.error.assert_called_with(
        "Service or timer file already exists for 'backup'"
    )


def test_new_dropin_common(tmp_path, monkeypatch, new_command):
    """new dropin creates common dropin."""
    monkeypatch.chdir(tmp_path)

    new_command.dropin(name="limits")

    dropin_file = tmp_path / ".fujin/systemd/common.d/limits.conf"
    assert dropin_file.exists()
    assert "[Service]" in dropin_file.read_text()


def test_new_dropin_service(tmp_path, monkeypatch, new_command):
    """new dropin --service creates service-specific dropin."""
    monkeypatch.chdir(tmp_path)

    new_command.dropin(name="override", service="web")

    dropin_file = tmp_path / ".fujin/systemd/web.service.d/override.conf"
    assert dropin_file.exists()
    assert "[Service]" in dropin_file.read_text()


def test_new_dropin_exists_error(tmp_path, monkeypatch, new_command):
    """new dropin errors if file already exists."""
    monkeypatch.chdir(tmp_path)

    # Create file first
    dropin_dir = tmp_path / ".fujin/systemd/common.d"
    dropin_dir.mkdir(parents=True)
    (dropin_dir / "limits.conf").touch()

    with pytest.raises(SystemExit) as exc:
        new_command.dropin(name="limits")

    assert exc.value.code == 1
    new_command.output.error.assert_called()
