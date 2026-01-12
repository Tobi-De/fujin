from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import cappa
import msgspec
import pytest

from fujin.commands.show import Show
from fujin.config import Config


@pytest.fixture
def show_config(tmp_path, monkeypatch):
    """Config fixture for show command tests."""
    monkeypatch.chdir(tmp_path)

    # Create .fujin directory structure
    fujin_dir = Path(".fujin")
    fujin_dir.mkdir()
    systemd_dir = fujin_dir / "systemd"
    systemd_dir.mkdir()
    (systemd_dir / "common.d").mkdir()

    # Create dummy service files
    (systemd_dir / "web.service").write_text(
        "[Unit]\nDescription={app_name} web\n[Service]\nUser={user}"
    )
    (systemd_dir / "web.socket").write_text(
        "[Socket]\nListenStream=/run/{app_name}/web.sock"
    )
    (systemd_dir / "worker.service").write_text("[Unit]\nDescription={app_name} worker")
    (systemd_dir / "common.d" / "base.conf").write_text("[Service]\nRestart=always")

    # Create Caddyfile
    (fujin_dir / "Caddyfile").write_text(
        "example.com {\n    reverse_proxy localhost:8000\n}"
    )

    # Create env file
    (tmp_path / ".env.prod").write_text("SECRET_KEY=supersecret\nDEBUG=False")

    return {
        "app": "testapp",
        "version": "1.0.0",
        "build_command": "echo build",
        "installation_mode": "python-package",
        "python_version": "3.11",
        "distfile": "dist/app.whl",
        "hosts": [{"address": "example.com", "user": "deploy", "envfile": ".env.prod"}],
    }


def test_show_lists_options_when_no_name(show_config):
    """fujin show (without args) lists available options."""
    config = msgspec.convert(show_config, type=Config)

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch.object(Show, "output", MagicMock()) as mock_output,
    ):
        show = Show(name=None)
        show()

        assert mock_output.info.called
        assert "Available options:" in mock_output.info.call_args[0][0]
        assert mock_output.output.called
        options = mock_output.output.call_args[0][0]
        assert "env" in options
        assert "caddy" in options
        assert "units" in options
        assert "web" in options
        assert "worker" in options


def test_show_env_redacted(show_config):
    """fujin show env redacts secrets by default."""
    config = msgspec.convert(show_config, type=Config)

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch.object(Show, "output", MagicMock()) as mock_output,
    ):
        show = Show(name="env", plain=False)
        show()

        # Check output
        output_text = mock_output.output.call_args[0][0]
        assert 'SECRET_KEY="***REDACTED***"' in output_text
        assert "DEBUG=False" in output_text
        assert mock_output.info.called  # Should show warning about redaction


def test_show_env_plain(show_config):
    """fujin show env --plain shows actual values."""
    config = msgspec.convert(show_config, type=Config)

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch.object(Show, "output", MagicMock()) as mock_output,
    ):
        show = Show(name="env", plain=True)
        show()

        output_text = mock_output.output.call_args[0][0]
        assert "SECRET_KEY=supersecret" in output_text
        assert "DEBUG=False" in output_text


def test_show_caddy(show_config):
    """fujin show caddy displays Caddyfile content."""
    config = msgspec.convert(show_config, type=Config)

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch.object(Show, "output", MagicMock()) as mock_output,
    ):
        show = Show(name="caddy")
        show()

        output_text = mock_output.output.call_args[0][0]
        assert "example.com {" in output_text
        assert "reverse_proxy localhost:8000" in output_text


def test_show_units_lists_all_files(show_config):
    """fujin show units displays all unit files."""
    config = msgspec.convert(show_config, type=Config)

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch.object(Show, "output", MagicMock()) as mock_output,
    ):
        show = Show(name="units")
        show()

        # Should show headers for each file
        headers = [call[0][0] for call in mock_output.info.call_args_list]
        assert any("web.service" in h for h in headers)
        assert any("web.socket" in h for h in headers)
        assert any("worker.service" in h for h in headers)
        assert any("common.d/base.conf" in h for h in headers)


def test_show_specific_unit_renders_templates(show_config):
    """fujin show web renders the service templates."""
    config = msgspec.convert(show_config, type=Config)

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch.object(Show, "output", MagicMock()) as mock_output,
    ):
        show = Show(name="web")
        show()

        # Capture all output calls
        outputs = [call[0][0] for call in mock_output.output.call_args_list]
        combined_output = "\n".join(outputs)

        # Check rendering of variables
        assert "Description=testapp web" in combined_output
        assert "User=deploy" in combined_output
        assert "ListenStream=/run/testapp/web.sock" in combined_output


def test_show_specific_unit_by_filename(show_config):
    """fujin show web.service works (full filename)."""
    config = msgspec.convert(show_config, type=Config)

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch.object(Show, "output", MagicMock()) as mock_output,
    ):
        show = Show(name="web.service")
        show()

        outputs = [call[0][0] for call in mock_output.output.call_args_list]
        combined_output = "\n".join(outputs)
        assert "Description=testapp web" in combined_output


def test_show_unknown_unit_raises_exit(show_config):
    """fujin show unknown raises cappa.Exit."""
    config = msgspec.convert(show_config, type=Config)

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch.object(Show, "output", MagicMock()),
    ):
        show = Show(name="unknown_service")
        with pytest.raises(cappa.Exit):
            show()


def test_show_caddy_missing(show_config, tmp_path):
    """fujin show caddy warns if Caddyfile is missing."""
    (tmp_path / ".fujin" / "Caddyfile").unlink()
    config = msgspec.convert(show_config, type=Config)

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch.object(Show, "output", MagicMock()) as mock_output,
    ):
        show = Show(name="caddy")
        show()
        assert mock_output.warning.called
        assert "No Caddyfile found" in mock_output.warning.call_args[0][0]


def test_show_env_missing(show_config, tmp_path):
    """fujin show env warns if envfile is missing/empty."""
    # Empty the env file
    (tmp_path / ".env.prod").write_text("")
    config = msgspec.convert(show_config, type=Config)

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch.object(Show, "output", MagicMock()) as mock_output,
    ):
        show = Show(name="env")
        show()
        assert mock_output.warning.called
