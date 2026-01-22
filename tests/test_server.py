"""Tests for server command - setup-ssh functionality.

Bootstrap, status, and create-user tests have been migrated to integration tests.
See tests/integration/test_server_bootstrap.py
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from fujin.commands.server import Server
from fujin.config import tomllib
from fujin.errors import SSHKeyError


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


def test_setup_ssh_fujin_toml_handling(tmp_path, monkeypatch):
    """setup-ssh creates or updates fujin.toml appropriately."""
    monkeypatch.chdir(tmp_path)

    # Create SSH key
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir(mode=0o700)
    (ssh_dir / "id_ed25519").write_text("key")

    # Test 1: Update existing fujin.toml
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
        assert any(
            "Updating existing fujin.toml" in str(call)
            for call in mock_output.info.call_args_list
        )

    # Test 2: Create new fujin.toml
    fujin_toml.unlink()  # Remove existing file

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
        assert fujin_toml.exists()
        config = tomllib.loads(fujin_toml.read_text())
        assert config["hosts"][0]["address"] == "server.example.com"
        assert config["hosts"][0]["user"] == "admin"
        assert any(
            "Creating new fujin.toml" in str(call)
            for call in mock_output.info.call_args_list
        )


@pytest.mark.parametrize(
    "sshpass_available,should_warn",
    [
        (True, False),  # sshpass available - no warning
        (False, True),  # sshpass unavailable - show warning
    ],
)
def test_setup_ssh_password_handling(
    tmp_path, monkeypatch, sshpass_available, should_warn
):
    """setup-ssh handles passwords with/without sshpass."""
    monkeypatch.chdir(tmp_path)

    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir(mode=0o700)
    (ssh_dir / "id_ed25519").write_text("key")

    sshpass_path = "/usr/bin/sshpass" if sshpass_available else None

    with (
        patch("fujin.commands.server.Prompt") as mock_prompt,
        patch("fujin.commands.server.subprocess.run") as mock_subprocess,
        patch("fujin.commands.server.Path.home", return_value=tmp_path),
        patch("fujin.commands.server.shutil.which", return_value=sshpass_path),
        patch.object(Server, "output", MagicMock()) as mock_output,
    ):
        mock_prompt.ask.side_effect = ["192.168.1.1", "user", "mypassword"]
        mock_subprocess.return_value = MagicMock(returncode=0)

        server = Server()
        server.setup_ssh()

        if sshpass_available:
            # Verify sshpass was used
            ssh_copy_call = mock_subprocess.call_args_list[-1][0][0]
            assert "sshpass" in ssh_copy_call
            assert "-p" in ssh_copy_call
            assert "mypassword" in ssh_copy_call
        else:
            # Verify warning was shown
            assert any(
                "sshpass not found" in str(call)
                for call in mock_output.warning.call_args_list
            )


@pytest.mark.parametrize(
    "error_type,mock_setup",
    [
        ("keyboard_interrupt", lambda: KeyboardInterrupt()),
        ("ssh_copy_failure", lambda: MagicMock(returncode=1)),
        ("keygen_failure", lambda: subprocess.CalledProcessError(1, "ssh-keygen")),
    ],
)
def test_setup_ssh_error_handling(tmp_path, monkeypatch, error_type, mock_setup):
    """setup-ssh handles various error scenarios."""
    monkeypatch.chdir(tmp_path)

    if error_type == "keyboard_interrupt":
        # Test keyboard interrupt
        with (
            patch("fujin.commands.server.Prompt") as mock_prompt,
            patch.object(Server, "output", MagicMock()),
        ):
            mock_prompt.ask.side_effect = mock_setup()

            server = Server()
            with pytest.raises(SystemExit) as exc_info:
                server.setup_ssh()
            assert exc_info.value.code == 0

    elif error_type == "ssh_copy_failure":
        # Test ssh-copy-id failure
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
            mock_subprocess.return_value = mock_setup()

            server = Server()
            with pytest.raises(SSHKeyError):
                server.setup_ssh()

    elif error_type == "keygen_failure":
        # Test ssh-keygen failure (no existing keys)
        with (
            patch("fujin.commands.server.Prompt") as mock_prompt,
            patch("fujin.commands.server.subprocess.run") as mock_subprocess,
            patch("fujin.commands.server.Path.home", return_value=tmp_path),
            patch.object(Server, "output", MagicMock()),
        ):
            mock_prompt.ask.side_effect = ["192.168.1.1", "user", ""]
            mock_subprocess.side_effect = [mock_setup()]

            server = Server()
            with pytest.raises(SSHKeyError):
                server.setup_ssh()


# ============================================================================
# Upgrade Command
# ============================================================================


def test_upgrade_command_all_success(minimal_config, mock_connection):
    """Test upgrade command when all components upgrade successfully."""
    mock_conn = MagicMock()

    # Mock different command outputs
    def run_side_effect(cmd, warn=False, hide=False):
        if "command -v caddy" in cmd:
            return ("", True)  # Caddy installed
        elif "command -v uv" in cmd:
            return ("", True)  # uv installed
        elif "caddy version" in cmd:
            return ("v2.8.0 h1:hash", True)  # Caddy version format
        elif "uv --version" in cmd:
            return ("uv 0.5.0", True)  # uv version format
        elif (
            "apt update" in cmd
            or "caddy upgrade" in cmd
            or "uv self update" in cmd
            or "uv python upgrade" in cmd
        ):
            return ("", True)
        return ("", True)

    mock_conn.run.side_effect = run_side_effect

    with (
        patch("fujin.config.Config.read", return_value=minimal_config),
        patch.object(
            Server, "connection", return_value=MagicMock(__enter__=lambda x: mock_conn)
        ),
        patch.object(Server, "output", MagicMock()) as mock_output,
    ):
        server = Server()
        server.upgrade()

        # Verify success message
        assert any(
            "All server components upgraded successfully" in str(call)
            for call in mock_output.success.call_args_list
        )


def test_upgrade_command_partial_failure(minimal_config, mock_connection):
    """Test upgrade command when some components fail."""
    mock_conn = MagicMock()

    # Caddy and Python upgrades fail, others succeed
    def run_side_effect(cmd, warn=False, hide=False):
        if "command -v caddy" in cmd:
            return ("", True)  # Caddy installed
        elif "command -v uv" in cmd:
            return ("", True)  # uv installed
        elif "caddy version" in cmd:
            return ("v2.8.0 h1:hash", True)  # Caddy version format
        elif "uv --version" in cmd:
            return ("uv 0.5.0", True)  # uv version format
        elif "apt update" in cmd:
            return ("", True)  # apt succeeds
        elif "caddy upgrade" in cmd:
            return ("", False)  # Caddy upgrade fails
        elif "uv self update" in cmd:
            return ("", True)  # uv succeeds
        elif "uv python upgrade" in cmd:
            return ("", False)  # Python upgrade fails
        return ("", True)

    mock_conn.run.side_effect = run_side_effect

    with (
        patch("fujin.config.Config.read", return_value=minimal_config),
        patch.object(
            Server, "connection", return_value=MagicMock(__enter__=lambda x: mock_conn)
        ),
        patch.object(Server, "output", MagicMock()) as mock_output,
    ):
        server = Server()
        server.upgrade()

        # Verify warning message
        assert any(
            "completed with some warnings" in str(call)
            for call in mock_output.warning.call_args_list
        )


def test_upgrade_command_components_not_installed(minimal_config, mock_connection):
    """Test upgrade command when components are not installed."""
    mock_conn = MagicMock()

    # Nothing installed
    def run_side_effect(cmd, warn=False, hide=False):
        if "command -v" in cmd:
            return ("", False)  # Nothing installed
        elif "apt update" in cmd:
            return ("", True)  # apt succeeds
        return ("", True)

    mock_conn.run.side_effect = run_side_effect

    with (
        patch("fujin.config.Config.read", return_value=minimal_config),
        patch.object(
            Server, "connection", return_value=MagicMock(__enter__=lambda x: mock_conn)
        ),
        patch.object(Server, "output", MagicMock()) as mock_output,
    ):
        server = Server()
        server.upgrade()

        # Verify informational messages about skipping
        assert any(
            "not installed" in str(call) for call in mock_output.info.call_args_list
        ) or any(
            "not installed" in str(call) for call in mock_output.warning.call_args_list
        )
