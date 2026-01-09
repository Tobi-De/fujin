"""Tests for server command."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import msgspec
import pytest

from fujin.commands.server import Server
from fujin.config import Config
from fujin.config import tomllib
from fujin.errors import SSHKeyError


@pytest.fixture
def base_config(tmp_path, monkeypatch):
    """Base config for server tests."""
    monkeypatch.chdir(tmp_path)

    return {
        "app": "testapp",
        "version": "1.0.0",
        "build_command": "echo building",
        "installation_mode": "python-package",
        "python_version": "3.11",
        "distfile": "dist/testapp-{version}-py3-none-any.whl",
        "processes": {"web": {"command": "gunicorn"}},
        "hosts": [{"address": "example.com", "user": "deploy"}],
        "webserver": {"enabled": True, "upstream": "localhost:8000"},
    }


@pytest.fixture
def config_without_webserver(base_config):
    """Config without webserver enabled."""
    base_config["webserver"]["enabled"] = False
    return base_config


# ============================================================================
# Info Command
# ============================================================================


def test_info_uses_fastfetch_when_available(base_config):
    """info uses fastfetch when available."""
    config = msgspec.convert(base_config, type=Config)
    mock_conn = MagicMock()

    mock_conn.run.side_effect = [
        ("", True),  # command -v fastfetch (available)
        ("", True),  # fastfetch
    ]

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch.object(Server, "connection") as mock_connection,
        patch.object(Server, "output", MagicMock()),
    ):
        mock_connection.return_value.__enter__.return_value = mock_conn
        mock_connection.return_value.__exit__.return_value = None

        server = Server()
        server.info()

        # Verify fastfetch was called
        calls = [call[0][0] for call in mock_conn.run.call_args_list]
        assert any("fastfetch" == cmd for cmd in calls)


def test_info_fallback_to_os_release_when_fastfetch_unavailable(base_config):
    """info falls back to /etc/os-release when fastfetch unavailable."""
    config = msgspec.convert(base_config, type=Config)
    mock_conn = MagicMock()

    mock_conn.run.side_effect = [
        ("", False),  # command -v fastfetch (not available)
        ("Ubuntu 22.04", True),  # cat /etc/os-release
    ]

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch.object(Server, "connection") as mock_connection,
        patch.object(Server, "output", MagicMock()) as mock_output,
    ):
        mock_connection.return_value.__enter__.return_value = mock_conn
        mock_connection.return_value.__exit__.return_value = None

        server = Server()
        server.info()

        # Verify os-release was read and displayed
        calls = [call[0][0] for call in mock_conn.run.call_args_list]
        assert any("/etc/os-release" in cmd for cmd in calls)
        mock_output.output.assert_called_with("Ubuntu 22.04")


# ============================================================================
# Bootstrap Command
# ============================================================================


def test_bootstrap_installs_dependencies(config_with_sites):
    """bootstrap installs system dependencies."""
    config = config_with_sites
    mock_conn = MagicMock()

    mock_conn.run.side_effect = [
        ("", True),  # apt update && upgrade && install
        ("", False),  # command -v uv (not installed)
        ("", True),  # install uv
        ("", True),  # uv tool install fastfetch
        ("", False),  # command -v caddy (not installed)
        ("", True),  # install caddy
    ]

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch.object(Server, "connection") as mock_connection,
        patch("fujin.commands.server.caddy.get_latest_gh_tag", return_value="v2.7.6"),
        patch(
            "fujin.commands.server.caddy.get_install_commands",
            return_value=["install caddy"],
        ),
        patch.object(Server, "output", MagicMock()) as mock_output,
    ):
        mock_connection.return_value.__enter__.return_value = mock_conn
        mock_connection.return_value.__exit__.return_value = None

        server = Server()
        server.bootstrap()

        # Verify apt commands were called
        calls = [call[0][0] for call in mock_conn.run.call_args_list]
        assert any("apt update" in cmd for cmd in calls)

        # Verify uv was installed
        assert any("astral.sh/uv/install.sh" in cmd for cmd in calls)

        # Verify caddy was installed
        assert any("install caddy" in cmd for cmd in calls)

        # Verify success message
        mock_output.success.assert_called_with(
            "Server bootstrap completed successfully!"
        )


def test_bootstrap_without_webserver_skips_caddy(config_without_webserver):
    """bootstrap skips Caddy installation when webserver disabled."""
    config = msgspec.convert(config_without_webserver, type=Config)
    mock_conn = MagicMock()

    mock_conn.run.side_effect = [
        ("", True),  # apt update
        ("", False),  # command -v uv
        ("", True),  # install uv
        ("", True),  # uv tool install fastfetch
    ]

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch.object(Server, "connection") as mock_connection,
        patch.object(Server, "output", MagicMock()),
    ):
        mock_connection.return_value.__enter__.return_value = mock_conn
        mock_connection.return_value.__exit__.return_value = None

        server = Server()
        server.bootstrap()

        # Verify caddy was not checked
        calls = [call[0][0] for call in mock_conn.run.call_args_list]
        assert not any("caddy" in cmd for cmd in calls)


def test_bootstrap_skips_uv_when_already_installed(config_with_sites):
    """bootstrap skips uv installation when already present."""
    config = config_with_sites
    mock_conn = MagicMock()

    mock_conn.run.side_effect = [
        ("", True),  # apt update
        ("", True),  # command -v uv (already installed)
        ("", True),  # uv tool install fastfetch
        ("", True),  # command -v caddy (already installed)
    ]

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch.object(Server, "connection") as mock_connection,
        patch.object(Server, "output", MagicMock()) as mock_output,
    ):
        mock_connection.return_value.__enter__.return_value = mock_conn
        mock_connection.return_value.__exit__.return_value = None

        server = Server()
        server.bootstrap()

        # Verify uv installation was skipped
        calls = [call[0][0] for call in mock_conn.run.call_args_list]
        assert not any("astral.sh/uv/install.sh" in cmd for cmd in calls)

        # Verify caddy warning was shown
        assert any(
            "already installed" in str(call)
            for call in mock_output.warning.call_args_list
        )


def test_bootstrap_warns_on_apt_failure(base_config):
    """bootstrap shows warning when apt update/upgrade fails."""
    config = msgspec.convert(base_config, type=Config)
    mock_conn = MagicMock()

    mock_conn.run.side_effect = [
        ("", False),  # apt update fails
        ("", True),  # command -v uv
        ("", True),  # uv tool install fastfetch
        ("", True),  # command -v caddy
    ]

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch.object(Server, "connection") as mock_connection,
        patch.object(Server, "output", MagicMock()) as mock_output,
    ):
        mock_connection.return_value.__enter__.return_value = mock_conn
        mock_connection.return_value.__exit__.return_value = None

        server = Server()
        server.bootstrap()

        # Verify warning was shown
        mock_output.warning.assert_called()
        assert any(
            "Failed to update and upgrade" in str(call)
            for call in mock_output.warning.call_args_list
        )


# ============================================================================
# Create User Command
# ============================================================================


def test_create_user_without_password(base_config):
    """create-user creates user without password."""
    config = msgspec.convert(base_config, type=Config)
    mock_conn = MagicMock()
    mock_conn.run.return_value = ("", True)

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch.object(Server, "connection") as mock_connection,
        patch.object(Server, "output", MagicMock()) as mock_output,
    ):
        mock_connection.return_value.__enter__.return_value = mock_conn
        mock_connection.return_value.__exit__.return_value = None

        server = Server()
        server.create_user(name="newuser", with_password=False)

        # Verify commands were called
        calls = [call[0][0] for call in mock_conn.run.call_args_list]
        full_cmd = calls[0]

        assert "adduser" in full_cmd
        assert "newuser" in full_cmd
        assert "chpasswd" not in full_cmd  # No password set

        # Verify success message
        mock_output.success.assert_called_with("New user newuser created successfully!")


def test_create_user_with_password(base_config):
    """create-user creates user with generated password."""
    config = msgspec.convert(base_config, type=Config)
    mock_conn = MagicMock()
    mock_conn.run.return_value = ("", True)

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch.object(Server, "connection") as mock_connection,
        patch("fujin.commands.server.secrets.token_hex", return_value="abc123def456"),
        patch.object(Server, "output", MagicMock()) as mock_output,
    ):
        mock_connection.return_value.__enter__.return_value = mock_conn
        mock_connection.return_value.__exit__.return_value = None

        server = Server()
        server.create_user(name="newuser", with_password=True)

        # Verify password was generated and displayed
        assert any(
            "Generated password: abc123def456" in str(call)
            for call in mock_output.success.call_args_list
        )

        # Verify chpasswd command was included
        calls = [call[0][0] for call in mock_conn.run.call_args_list]
        full_cmd = calls[0]
        assert "chpasswd" in full_cmd
        assert "newuser:abc123def456" in full_cmd


def test_create_user_sets_sudo_access(base_config):
    """create-user grants sudo NOPASSWD access."""
    config = msgspec.convert(base_config, type=Config)
    mock_conn = MagicMock()
    mock_conn.run.return_value = ("", True)

    with (
        patch("fujin.config.Config.read", return_value=config),
        patch.object(Server, "connection") as mock_connection,
        patch.object(Server, "output", MagicMock()),
    ):
        mock_connection.return_value.__enter__.return_value = mock_conn
        mock_connection.return_value.__exit__.return_value = None

        server = Server()
        server.create_user(name="newuser", with_password=False)

        # Verify sudo access was granted
        calls = [call[0][0] for call in mock_conn.run.call_args_list]
        full_cmd = calls[0]
        assert "NOPASSWD:ALL" in full_cmd
        assert "/etc/sudoers" in full_cmd


# ============================================================================
# Setup SSH Command
# ============================================================================


def test_setup_ssh_with_existing_key(tmp_path, monkeypatch):
    """setup-ssh uses existing SSH key."""
    monkeypatch.chdir(tmp_path)

    # Create existing SSH key
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir(mode=0o700)
    key_file = ssh_dir / "id_ed25519"
    key_file.write_text("fake key")

    with (
        patch("fujin.commands.server.Prompt") as mock_prompt,
        patch("fujin.commands.server.subprocess.run") as mock_subprocess,
        patch("fujin.commands.server.Path.home", return_value=tmp_path),
        patch.object(Server, "output", MagicMock()) as mock_output,
    ):
        mock_prompt.ask.side_effect = [
            "192.168.1.100",  # IP
            "deploy",  # username
            "",  # password (empty)
        ]

        # Mock ssh-copy-id success
        mock_subprocess.return_value = MagicMock(returncode=0)

        server = Server()
        server.setup_ssh()

        # Verify existing key was used
        assert any(
            "Using existing SSH key" in str(call)
            for call in mock_output.info.call_args_list
        )

        # Verify ssh-copy-id was called
        mock_subprocess.assert_called()
        ssh_copy_call = mock_subprocess.call_args[0][0]
        assert "ssh-copy-id" in ssh_copy_call
        assert "deploy@192.168.1.100" in ssh_copy_call


def test_setup_ssh_generates_new_key_when_none_exists(tmp_path, monkeypatch):
    """setup-ssh generates new SSH key when none exists."""
    monkeypatch.chdir(tmp_path)

    with (
        patch("fujin.commands.server.Prompt") as mock_prompt,
        patch("fujin.commands.server.subprocess.run") as mock_subprocess,
        patch("fujin.commands.server.Path.home", return_value=tmp_path),
        patch.object(Server, "output", MagicMock()) as mock_output,
    ):
        mock_prompt.ask.side_effect = [
            "192.168.1.100",
            "root",
            "",
        ]

        # Mock subprocess calls - need different returns for ssh-keygen and ssh-copy-id
        mock_subprocess.side_effect = [
            MagicMock(returncode=0),  # ssh-keygen
            MagicMock(returncode=0),  # ssh-copy-id
        ]

        server = Server()
        server.setup_ssh()

        # Verify both calls were made
        assert mock_subprocess.call_count == 2

        # First call should be ssh-keygen
        keygen_call = mock_subprocess.call_args_list[0][0][0]
        assert "ssh-keygen" in keygen_call
        assert "-t" in keygen_call
        assert "ed25519" in keygen_call

        # Verify success message
        assert any(
            "Generated SSH key" in str(call)
            for call in mock_output.success.call_args_list
        )


def test_setup_ssh_updates_existing_fujin_toml(tmp_path, monkeypatch):
    """setup-ssh updates existing fujin.toml."""
    monkeypatch.chdir(tmp_path)

    # Create existing fujin.toml
    fujin_toml = tmp_path / "fujin.toml"
    fujin_toml.write_text(
        """
app = "myapp"
version = "1.0.0"

[[hosts]]
domain_name = "old.example.com"
user = "olduser"
"""
    )

    # Create SSH key
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir(mode=0o700)
    (ssh_dir / "id_ed25519").write_text("key")

    with (
        patch("fujin.commands.server.Prompt") as mock_prompt,
        patch("fujin.commands.server.subprocess.run") as mock_subprocess,
        patch("fujin.commands.server.Path.home", return_value=tmp_path),
        patch.object(Server, "output", MagicMock()) as mock_output,
    ):
        mock_prompt.ask.side_effect = ["10.0.0.5", "deploy", ""]
        mock_subprocess.return_value = MagicMock(returncode=0)

        server = Server()
        server.setup_ssh()

        # Verify fujin.toml was updated
        config = tomllib.loads(fujin_toml.read_text())
        assert config["hosts"][0]["address"] == "10.0.0.5"
        assert config["hosts"][0]["user"] == "deploy"

        # Verify update message
        assert any(
            "Updating existing fujin.toml" in str(call)
            for call in mock_output.info.call_args_list
        )


def test_setup_ssh_creates_new_fujin_toml(tmp_path, monkeypatch):
    """setup-ssh creates new fujin.toml when none exists."""
    monkeypatch.chdir(tmp_path)

    # No fujin.toml

    # Create SSH key
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir(mode=0o700)
    (ssh_dir / "id_ed25519").write_text("key")

    with (
        patch("fujin.commands.server.Prompt") as mock_prompt,
        patch("fujin.commands.server.subprocess.run") as mock_subprocess,
        patch("fujin.commands.server.Path.home", return_value=tmp_path),
        patch.object(Server, "output", MagicMock()) as mock_output,
    ):
        mock_prompt.ask.side_effect = ["server.example.com", "admin", ""]
        mock_subprocess.return_value = MagicMock(returncode=0)

        server = Server()
        server.setup_ssh()

        # Verify fujin.toml was created
        fujin_toml = tmp_path / "fujin.toml"
        assert fujin_toml.exists()

        # Verify config content
        config = tomllib.loads(fujin_toml.read_text())
        assert config["hosts"][0]["address"] == "server.example.com"
        assert config["hosts"][0]["user"] == "admin"

        # Verify creation message
        assert any(
            "Creating new fujin.toml" in str(call)
            for call in mock_output.info.call_args_list
        )


def test_setup_ssh_uses_sshpass_with_password(tmp_path, monkeypatch):
    """setup-ssh uses sshpass when password provided and available."""
    monkeypatch.chdir(tmp_path)

    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir(mode=0o700)
    (ssh_dir / "id_ed25519").write_text("key")

    with (
        patch("fujin.commands.server.Prompt") as mock_prompt,
        patch("fujin.commands.server.subprocess.run") as mock_subprocess,
        patch("fujin.commands.server.Path.home", return_value=tmp_path),
        patch("fujin.commands.server.shutil.which", return_value="/usr/bin/sshpass"),
        patch.object(Server, "output", MagicMock()),
    ):
        mock_prompt.ask.side_effect = ["192.168.1.1", "user", "mypassword"]
        mock_subprocess.return_value = MagicMock(returncode=0)

        server = Server()
        server.setup_ssh()

        # Verify sshpass was used
        ssh_copy_call = mock_subprocess.call_args_list[-1][0][0]
        assert "sshpass" in ssh_copy_call
        assert "-p" in ssh_copy_call
        assert "mypassword" in ssh_copy_call


def test_setup_ssh_warns_when_sshpass_unavailable(tmp_path, monkeypatch):
    """setup-ssh warns when password provided but sshpass unavailable."""
    monkeypatch.chdir(tmp_path)

    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir(mode=0o700)
    (ssh_dir / "id_ed25519").write_text("key")

    with (
        patch("fujin.commands.server.Prompt") as mock_prompt,
        patch("fujin.commands.server.subprocess.run") as mock_subprocess,
        patch("fujin.commands.server.Path.home", return_value=tmp_path),
        patch("fujin.commands.server.shutil.which", return_value=None),
        patch.object(Server, "output", MagicMock()) as mock_output,
    ):
        mock_prompt.ask.side_effect = ["192.168.1.1", "user", "mypassword"]
        mock_subprocess.return_value = MagicMock(returncode=0)

        server = Server()
        server.setup_ssh()

        # Verify warning was shown
        assert any(
            "sshpass not found" in str(call)
            for call in mock_output.warning.call_args_list
        )


def test_setup_ssh_handles_keyboard_interrupt(tmp_path, monkeypatch):
    """setup-ssh handles Ctrl+C gracefully."""
    monkeypatch.chdir(tmp_path)

    with (
        patch("fujin.commands.server.Prompt") as mock_prompt,
        patch.object(Server, "output", MagicMock()),
    ):
        mock_prompt.ask.side_effect = KeyboardInterrupt

        server = Server()

        with pytest.raises(SystemExit) as exc_info:
            server.setup_ssh()

        assert exc_info.value.code == 0


def test_setup_ssh_raises_on_ssh_copy_failure(tmp_path, monkeypatch):
    """setup-ssh raises error when ssh-copy-id fails."""
    monkeypatch.chdir(tmp_path)

    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir(mode=0o700)
    (ssh_dir / "id_ed25519").write_text("key")

    with (
        patch("fujin.commands.server.Prompt") as mock_prompt,
        patch("fujin.commands.server.subprocess.run") as mock_subprocess,
        patch("fujin.commands.server.Path.home", return_value=tmp_path),
        patch.object(Server, "output", MagicMock()),
    ):
        mock_prompt.ask.side_effect = ["192.168.1.1", "user", ""]
        mock_subprocess.return_value = MagicMock(returncode=1)  # Failed

        server = Server()

        with pytest.raises(SSHKeyError):
            server.setup_ssh()


def test_setup_ssh_raises_on_keygen_failure(tmp_path, monkeypatch):
    """setup-ssh raises error when ssh-keygen fails."""
    monkeypatch.chdir(tmp_path)

    # No existing keys

    with (
        patch("fujin.commands.server.Prompt") as mock_prompt,
        patch("fujin.commands.server.subprocess.run") as mock_subprocess,
        patch("fujin.commands.server.Path.home", return_value=tmp_path),
        patch.object(Server, "output", MagicMock()),
    ):
        mock_prompt.ask.side_effect = ["192.168.1.1", "user", ""]

        # Mock ssh-keygen failure
        mock_subprocess.side_effect = [subprocess.CalledProcessError(1, "ssh-keygen")]

        server = Server()

        with pytest.raises(SSHKeyError):
            server.setup_ssh()
