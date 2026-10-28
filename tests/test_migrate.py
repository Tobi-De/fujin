"""Tests for the migrate command (legacy config -> file-based structure)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fujin.commands.migrate import Migrate
from fujin.config import tomllib

LEGACY_TOML = """\
app = "myapp"
version = "1.0.0"
build_command = "uv build"
distfile = "dist/myapp-{version}-py3-none-any.whl"
installation_mode = "python-package"
release_command = "myapp migrate"

[processes.web]
command = ".venv/bin/gunicorn myapp.wsgi:app --bind localhost:8000"
listen = "localhost:8000"

[processes.worker]
command = ".venv/bin/celery -A myapp worker"
replicas = 2

[[sites]]
domains = ["myapp.com"]
routes = { "/" = "web" }

[[hosts]]
address = "myapp.com"
user = "deploy"
"""


@pytest.fixture
def legacy_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "fujin.toml").write_text(LEGACY_TOML)
    return tmp_path


def _run_migrate() -> MagicMock:
    with patch.object(Migrate, "output", MagicMock()) as output:
        Migrate()()
    return output


def _all_generated_text(project: Path) -> dict[str, str]:
    return {
        p.relative_to(project).as_posix(): p.read_text()
        for p in project.rglob("*")
        if p.is_file()
    }


def test_migrate_derives_exec_start_from_process_command(legacy_project):
    _run_migrate()

    service = (legacy_project / ".fujin/systemd/web.service").read_text()
    assert (
        "ExecStart={app_dir}/current/venv/bin/gunicorn myapp.wsgi:app --bind localhost:8000"
        in service
    )
    assert "ExecStartPre" not in service


def test_migrate_generated_files_use_deploy_placeholders(legacy_project):
    _run_migrate()

    generated = _all_generated_text(legacy_project)
    assert "fujin.toml" in generated
    for relpath, text in generated.items():
        assert "{install_dir}" not in text, relpath
        assert ".venv/" not in text.replace("{app_dir}/current/venv/", ""), relpath


def test_migrate_rewrites_fujin_toml(legacy_project):
    _run_migrate()

    config = tomllib.loads((legacy_project / "fujin.toml").read_text())

    assert "processes" not in config
    assert "sites" not in config
    assert "webserver" not in config
    assert "release_command" not in config
    assert config["replicas"] == {"worker": 2}
    assert config["hooks"]["post_install"] == ["{app_dir}/venv/bin/myapp migrate"]
    assert config["hosts"] == [{"address": "myapp.com", "user": "deploy"}]


def test_migrate_generates_template_unit_for_replicas(legacy_project):
    _run_migrate()

    assert not (legacy_project / ".fujin/systemd/worker.service").exists()
    worker = (legacy_project / ".fujin/systemd/worker@.service").read_text()
    assert "Description={app_name} worker %i" in worker
    assert "ExecStart={app_dir}/current/venv/bin/celery -A myapp worker" in worker


def test_migrate_generates_caddyfile(legacy_project):
    _run_migrate()

    caddyfile = (legacy_project / ".fujin/Caddyfile").read_text()
    assert "# Caddyfile for {app_name}" in caddyfile
    assert "myapp.com {" in caddyfile
    assert "reverse_proxy localhost:8000" in caddyfile
    assert "{{" not in caddyfile


def test_migrate_binary_mode_and_verbatim_hook_command(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "fujin.toml").write_text(
        """\
app = "myapp"
build_command = "cargo build --release"
distfile = "target/release/myapp"
installation_mode = "binary"
release_command = "sudo systemctl reload caddy && ./myapp migrate --no-input"

[processes.web]
command = "./myapp serve --port 8000"
socket = true
timer = { on_calendar = "*-*-* 03:00:00", persistent = true }

[[hosts]]
address = "myapp.com"
user = "deploy"
"""
    )

    output = _run_migrate()

    web = (tmp_path / ".fujin/systemd/web.service").read_text()
    assert "ExecStart={app_dir}/current/bin/myapp serve --port 8000" in web

    socket = (tmp_path / ".fujin/systemd/web.socket").read_text()
    assert "PartOf={app_name}-web.service" in socket
    assert "ListenStream=/run/{app_name}/web.sock" in socket
    assert "SocketUser={app_user}" in socket
    assert "{{" not in socket

    timer = (tmp_path / ".fujin/systemd/web.timer").read_text()
    assert "Description={app_name} web timer" in timer
    assert "OnCalendar=*-*-* 03:00:00" in timer
    assert "Persistent=true" in timer
    assert (tmp_path / ".fujin/systemd/web.timer").exists()

    config = tomllib.loads((tmp_path / "fujin.toml").read_text())
    assert config["hooks"]["post_install"] == [
        "sudo systemctl reload caddy",
        "{app_dir}/bin/myapp migrate --no-input",
    ]

    # Exactly one warning for the verbatim (non-app-binary) command.
    assert output.warning.call_count == 1
    assert "sudo systemctl reload caddy" in output.warning.call_args[0][0]


def test_migrate_template_service_drops_singleton_partof(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "fujin.toml").write_text(
        """\
app = "myapp"
build_command = "uv build"
distfile = "dist/myapp-{version}-py3-none-any.whl"
installation_mode = "python-package"

[processes.worker]
command = ".venv/bin/celery -A myapp worker"
replicas = 3
socket = true

[[hosts]]
address = "myapp.com"
user = "deploy"
"""
    )

    _run_migrate()

    socket = (tmp_path / ".fujin/systemd/worker.socket").read_text()
    assert "PartOf" not in socket
    assert "ListenStream=/run/{app_name}/worker.sock" in socket
    assert not (tmp_path / ".fujin/systemd/worker@.socket").exists()


@pytest.mark.parametrize(
    ("command", "mode", "expected"),
    [
        (
            ".venv/bin/python -m myapp.web",
            "python-package",
            "{app_dir}/current/venv/bin/python -m myapp.web",
        ),
        (
            "./myapp serve --port 8000",
            "binary",
            "{app_dir}/current/bin/myapp serve --port 8000",
        ),
        (
            "myapp serve",
            "binary",
            "{app_dir}/current/bin/myapp serve",
        ),
        (
            "{app_dir}/current/venv/bin/gunicorn app:app",
            "python-package",
            "{app_dir}/current/venv/bin/gunicorn app:app",
        ),
    ],
)
def test_derive_exec_start(command, mode, expected):
    assert Migrate()._derive_exec_start(command, mode, "myapp") == expected


@pytest.mark.parametrize(
    ("release_command", "mode", "expected_hooks"),
    [
        (
            "myapp migrate",
            "python-package",
            ["{app_dir}/venv/bin/myapp migrate"],
        ),
        (
            "./myapp migrate --no-input",
            "binary",
            ["{app_dir}/bin/myapp migrate --no-input"],
        ),
        (
            "{app_dir}/current/bin/myapp migrate",
            "binary",
            ["{app_dir}/bin/myapp migrate"],
        ),
        (
            "{install_dir}/{app_name} migrate",
            "binary",
            ["{app_dir}/bin/myapp migrate"],
        ),
        (
            "sudo systemctl reload caddy && myapp migrate",
            "python-package",
            ["sudo systemctl reload caddy", "{app_dir}/venv/bin/myapp migrate"],
        ),
    ],
)
def test_convert_release_command(release_command, mode, expected_hooks):
    hooks, verbatim = Migrate()._convert_release_command(release_command, "myapp", mode)
    assert hooks == expected_hooks
    assert verbatim == [cmd for cmd in hooks if "sudo" in cmd]
