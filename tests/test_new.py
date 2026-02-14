"""Tests for new command."""

from __future__ import annotations

import pytest

from fujin.commands.new import New


def test_new_service_creates_file(tmp_path, monkeypatch, mock_output):
    """new service creates a service file."""
    monkeypatch.chdir(tmp_path)

    cmd = New(kind="service", name="worker")
    cmd()

    service_file = tmp_path / ".fujin/systemd/worker.service"
    assert service_file.exists()
    content = service_file.read_text()
    assert "[Unit]" in content
    assert "Description={app_name} worker" in content
    assert "[Service]" in content


def test_new_service_exists_error(tmp_path, monkeypatch, mock_output):
    """new service errors if file already exists."""
    monkeypatch.chdir(tmp_path)

    # Create file first
    systemd_dir = tmp_path / ".fujin/systemd"
    systemd_dir.mkdir(parents=True)
    (systemd_dir / "worker.service").touch()

    cmd = New(kind="service", name="worker")

    with pytest.raises(SystemExit) as exc:
        cmd()

    assert exc.value.code == 1


def test_new_timer_creates_files(tmp_path, monkeypatch, mock_output):
    """new timer creates service and timer files."""
    monkeypatch.chdir(tmp_path)

    cmd = New(kind="timer", name="cleanup")
    cmd()

    service_file = tmp_path / ".fujin/systemd/cleanup.service"
    timer_file = tmp_path / ".fujin/systemd/cleanup.timer"

    assert service_file.exists()
    assert timer_file.exists()

    service_content = service_file.read_text()
    assert "Type=oneshot" in service_content

    timer_content = timer_file.read_text()
    assert "[Timer]" in timer_content
    assert "OnCalendar=daily" in timer_content


def test_new_timer_exists_error(tmp_path, monkeypatch, mock_output):
    """new timer errors if files already exist."""
    monkeypatch.chdir(tmp_path)

    # Create file first
    systemd_dir = tmp_path / ".fujin/systemd"
    systemd_dir.mkdir(parents=True)
    (systemd_dir / "backup.timer").touch()

    cmd = New(kind="timer", name="backup")

    with pytest.raises(SystemExit) as exc:
        cmd()

    assert exc.value.code == 1


def test_new_socket_creates_file(tmp_path, monkeypatch, mock_output):
    """new socket creates a socket file."""
    monkeypatch.chdir(tmp_path)

    cmd = New(kind="socket", name="web")
    cmd()

    socket_file = tmp_path / ".fujin/systemd/web.socket"
    assert socket_file.exists()
    content = socket_file.read_text()
    assert "[Unit]" in content
    assert "Description={app_name} web socket" in content
    assert "[Socket]" in content
    assert "ListenStream=/run/{app_name}/web.sock" in content
    assert "SocketUser={app_user}" in content
    assert "SocketMode=0660" in content


def test_new_socket_exists_error(tmp_path, monkeypatch, mock_output):
    """new socket errors if file already exists."""
    monkeypatch.chdir(tmp_path)

    # Create file first
    systemd_dir = tmp_path / ".fujin/systemd"
    systemd_dir.mkdir(parents=True)
    (systemd_dir / "web.socket").touch()

    cmd = New(kind="socket", name="web")

    with pytest.raises(SystemExit) as exc:
        cmd()

    assert exc.value.code == 1


def test_new_dropin_common(tmp_path, monkeypatch, mock_output):
    """new dropin creates common dropin."""
    monkeypatch.chdir(tmp_path)

    cmd = New(kind="dropin", name="limits")
    cmd()

    dropin_file = tmp_path / ".fujin/systemd/common.d/limits.conf"
    assert dropin_file.exists()
    assert "[Service]" in dropin_file.read_text()


def test_new_dropin_service(tmp_path, monkeypatch, mock_output):
    """new dropin --service creates service-specific dropin."""
    monkeypatch.chdir(tmp_path)

    cmd = New(kind="dropin", name="override", service="web")
    cmd()

    dropin_file = tmp_path / ".fujin/systemd/web.service.d/override.conf"
    assert dropin_file.exists()
    assert "[Service]" in dropin_file.read_text()


def test_new_dropin_exists_error(tmp_path, monkeypatch, mock_output):
    """new dropin errors if file already exists."""
    monkeypatch.chdir(tmp_path)

    # Create file first
    dropin_dir = tmp_path / ".fujin/systemd/common.d"
    dropin_dir.mkdir(parents=True)
    (dropin_dir / "limits.conf").touch()

    cmd = New(kind="dropin", name="limits")

    with pytest.raises(SystemExit) as exc:
        cmd()

    assert exc.value.code == 1
