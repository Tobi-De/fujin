"""Tests for the server commands.

Bootstrap, status and end-to-end create-user/deploy flows live in
tests/integration/test_server_bootstrap.py.  The unit tests here cover
setup-ssh, upgrade handling and the scoped-sudoers transactions.
"""

from __future__ import annotations

import shlex
import subprocess
from unittest.mock import MagicMock, patch

import cappa
import pytest

from fujin import sudoers
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
            "apt-get update" in cmd
            or "caddy upgrade" in cmd
            or "uv self update" in cmd
            or "uv python upgrade" in cmd
        ):
            return ("", True)
        return ("", True)

    mock_conn.run.side_effect = run_side_effect

    with (
        patch("fujin.config.Config.read", return_value=minimal_config),
        patch(
            "fujin.connection.connection",
            return_value=MagicMock(__enter__=lambda x: mock_conn),
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
        elif "apt-get update" in cmd:
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
        patch(
            "fujin.connection.connection",
            return_value=MagicMock(__enter__=lambda x: mock_conn),
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
        elif "apt-get update" in cmd:
            return ("", True)  # apt succeeds
        return ("", True)

    mock_conn.run.side_effect = run_side_effect

    with (
        patch("fujin.config.Config.read", return_value=minimal_config),
        patch(
            "fujin.connection.connection",
            return_value=MagicMock(__enter__=lambda x: mock_conn),
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


# ============================================================================
# create-user / harden (scoped sudoers, issue #93)
# ============================================================================


class FakeConn:
    """Records commands; exact-match responses first, then substring match.

    Deliberately has no ``run_script``: admin scripts must go through the
    inline PTY ``run`` path, so a regression to the non-PTY spool path fails
    here with ``AttributeError`` instead of silently passing.
    """

    def __init__(self, exact=None, contains=None):
        self.commands: list[str] = []
        self.calls: list[tuple[str, dict]] = []
        self.exact = exact or {}
        self.contains = contains or {}

    def run(self, command, **kwargs):
        self.commands.append(command)
        self.calls.append((command, kwargs))
        if command in self.exact:
            return self.exact[command]
        for needle, result in self.contains.items():
            if needle in command:
                return result
        return ("", True)


def _run_with_fake(conn: FakeConn, config, method, *args, **kwargs):
    with (
        patch("fujin.config.Config.read", return_value=config),
        patch(
            "fujin.connection.connection",
            return_value=MagicMock(
                __enter__=lambda x: conn, __exit__=lambda *args: False
            ),
        ),
        patch.object(Server, "output", MagicMock()) as mock_output,
    ):
        server = Server()
        getattr(server, method)(*args, **kwargs)
    return mock_output


SUDOERS_WITH_INCLUDE = (
    "#includedir /etc/sudoers.d\n\nroot ALL=(ALL:ALL) ALL\n%sudo ALL=(ALL:ALL) ALL\n"
)

SUDOERS_WITH_LEGACY = (
    "#includedir /etc/sudoers.d/\n"
    "\n"
    "root ALL=(ALL:ALL) ALL\n"
    "deploy ALL=(ALL) NOPASSWD:ALL\n"
    "%sudo ALL=(ALL:ALL) ALL\n"
)

CAT = "sudo cat /etc/sudoers"


def _admin_cmd(script: str) -> str:
    """The inline PTY wrapper admin scripts are sent through."""
    return f"sudo bash -c {shlex.quote(script)}"


def _install_cmd(name: str = "newdeploy") -> str:
    rule = sudoers.render_rule(name, "testapp", "testapp")
    return _admin_cmd(sudoers.install_dropin_script(name, rule))


def _harden_cmd() -> str:
    rule = sudoers.render_rule("deploy", "testapp", "testapp")
    return _admin_cmd(sudoers.harden_script("deploy", "testapp", "testapp", rule))


def test_create_user_installs_scoped_dropin_and_never_appends_nopasswd(
    minimal_config,
):
    cmd = _install_cmd()
    conn = FakeConn(
        exact={CAT: (SUDOERS_WITH_INCLUDE, True), cmd: ("FUJIN_DROPIN_OK", True)}
    )
    output = _run_with_fake(conn, minimal_config, "create_user", "newdeploy")

    assert conn.commands[0] == CAT
    creation = conn.commands[1]
    assert "sudo adduser --disabled-password" in creation
    assert "NOPASSWD" not in creation
    assert "/etc/sudoers" not in creation
    assert cmd in conn.commands
    # The root script goes inline over a PTY: sudo may prompt for a password
    # or require a tty, and it must never be staged in /tmp.
    assert conn.calls[-1] == (cmd, {"warn": True, "pty": True})

    joined = "\n".join(conn.commands)
    assert "NOPASSWD:ALL" not in joined
    assert "install -m 0440 -o root -g root" in cmd
    assert "/etc/sudoers.d/fujin-newdeploy" in cmd
    # Drop-in content is installed, never appended to /etc/sudoers.
    assert not any("tee -a /etc/sudoers" in c for c in conn.commands)
    assert output.success.called


def test_create_user_aborts_with_instructions_when_sudoers_d_is_not_included(
    minimal_config,
):
    conn = FakeConn(exact={CAT: ("root ALL=(ALL:ALL) ALL\n", True)})
    with pytest.raises(cappa.Exit) as exc:
        _run_with_fake(conn, minimal_config, "create_user", "newdeploy")

    assert exc.value.code == 1
    assert conn.commands == [CAT]
    assert "includedir" in str(exc.value.message)


def test_create_user_rejects_dot_in_username(minimal_config):
    conn = FakeConn()
    with pytest.raises(cappa.Exit) as exc:
        _run_with_fake(conn, minimal_config, "create_user", "deploy.prod")

    assert exc.value.code == 1
    assert conn.commands == []


def test_create_user_does_not_report_success_when_validation_fails(minimal_config):
    cmd = _install_cmd()
    conn = FakeConn(
        exact={
            CAT: (SUDOERS_WITH_INCLUDE, True),
            cmd: ("FUJIN_DROPIN_FAILED reason=error", False),
        }
    )
    with pytest.raises(cappa.Exit) as exc:
        _run_with_fake(conn, minimal_config, "create_user", "newdeploy")

    assert exc.value.code == 1
    assert "restored the previous state" in str(exc.value.message)


def test_harden_dry_run_mutates_nothing(minimal_config):
    conn = FakeConn(exact={CAT: (SUDOERS_WITH_LEGACY, True)})
    output = _run_with_fake(conn, minimal_config, "harden", dry_run=True)

    assert conn.commands == [CAT]
    printed = "\n".join(
        str(call) for call in output.output.call_args_list + output.info.call_args_list
    )
    assert "deploy ALL=(root) NOPASSWD: FUJIN_DEPLOY" in printed
    assert "deploy ALL=(ALL) NOPASSWD:ALL" in printed
    assert "no changes made" in printed.lower()


def test_harden_runs_one_root_transaction_and_removes_exact_legacy_line(
    minimal_config,
):
    cmd = _harden_cmd()
    conn = FakeConn(
        exact={
            cmd: (
                "FUJIN_HARDEN_OK backup=/etc/sudoers.fujin-backup-20260101T000000Z",
                True,
            ),
        }
    )
    output = _run_with_fake(conn, minimal_config, "harden")

    # Exactly one root shell (one round trip); no client-side read, backup
    # or replace step that could leave a half-migrated box behind.
    assert conn.commands == [cmd]
    assert conn.calls == [(cmd, {"warn": True, "pty": True})]
    assert "trap cleanup EXIT" in cmd
    assert 'cp -a "$BACKUP" "$SUDOERS"' in cmd
    assert "sed -E" in cmd
    assert "sudo -l -U deploy" in cmd
    assert any(
        "Backup of the previous sudoers file" in str(call)
        for call in output.info.call_args_list
    )
    assert output.success.called


def test_harden_skips_sudoers_rewrite_when_no_legacy_rule(minimal_config):
    cmd = _harden_cmd()
    conn = FakeConn(exact={cmd: ("FUJIN_HARDEN_OK backup=none", True)})
    output = _run_with_fake(conn, minimal_config, "harden")

    assert conn.commands == [cmd]
    printed = "\n".join(str(call) for call in output.info.call_args_list)
    assert "was not modified" in printed


def test_harden_aborts_when_sudoers_d_is_not_included(minimal_config):
    cmd = _harden_cmd()
    conn = FakeConn(
        exact={
            cmd: ("FUJIN_HARDEN_FAILED reason=includedir-missing", False),
        }
    )
    with pytest.raises(cappa.Exit) as exc:
        _run_with_fake(conn, minimal_config, "harden")

    assert exc.value.code == 1
    assert conn.commands == [cmd]
    assert "rolled back" in str(exc.value.message)


def test_harden_post_install_validation_failure_rolls_back_without_success_claim(
    minimal_config,
):
    cmd = _harden_cmd()
    conn = FakeConn(
        exact={
            # The final `visudo -c` (after the mv) failed inside the script.
            cmd: ("FUJIN_HARDEN_FAILED reason=visudo", False),
        }
    )
    with pytest.raises(cappa.Exit) as exc:
        _run_with_fake(conn, minimal_config, "harden")

    assert exc.value.code == 1
    assert "rolled back" in str(exc.value.message)
    assert conn.commands == [cmd]
    # The script, not the client, owns the restore.
    assert 'cp -a "$BACKUP" "$SUDOERS"' in cmd
    assert 'mv -f "$DROPIN_BAK" "$DROPIN"' in cmd
