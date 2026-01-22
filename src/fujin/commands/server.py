import shlex
import shutil
import secrets
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import cappa
from rich.prompt import Prompt
import tomli_w

from fujin import caddy
from fujin.commands import BaseCommand
from fujin.config import tomllib
from fujin.errors import SSHKeyError


@cappa.command(
    help="Manage server operations",
)
@dataclass
class Server(BaseCommand):
    """
    Examples:
      fujin server setup-ssh      Interactive SSH key setup
      fujin server bootstrap      Setup server with dependencies and Caddy
      fujin server status         Show server system information
    """

    @cappa.command(help="Display information about the host system")
    def status(self):
        with self.connection() as conn:
            _, result_ok = conn.run("command -v fastfetch", warn=True, hide=True)
            if result_ok:
                conn.run("fastfetch", pty=True)
            else:
                self.output.output(conn.run("cat /etc/os-release", hide=True)[0])

    @cappa.command(help="Setup uv, web proxy, and install necessary dependencies")
    def bootstrap(self):
        with self.connection() as conn:
            self.output.info("Bootstrapping server...")

            self._upgrade_system(conn, packages="sqlite3 curl rsync")
            _, result_ok = conn.run("command -v uv", warn=True)
            if not result_ok:
                self.output.info("Installing uv tool...")
                conn.run(
                    "curl -LsSf https://astral.sh/uv/install.sh | sh && uv tool update-shell"
                )
            conn.run("uv tool install fastfetch-bin --force")

            self.output.info("Setting up fujin group...")
            conn.run("sudo groupadd -f fujin", pty=True)

            self.output.info("Setting up /opt/fujin directory...")
            conn.run("sudo mkdir -p /opt/fujin", pty=True)
            conn.run("sudo chown root:fujin /opt/fujin", pty=True)
            conn.run("sudo chmod 775 /opt/fujin", pty=True)

            self.output.info("Setting up shared Python directory...")
            conn.run("sudo mkdir -p /opt/fujin/.python", pty=True)
            conn.run("sudo chown root:fujin /opt/fujin/.python", pty=True)
            conn.run("sudo chmod 775 /opt/fujin/.python", pty=True)

            self.output.info(f"Adding {self.selected_host.user} to fujin group...")
            conn.run(f"sudo usermod -aG fujin {self.selected_host.user}", pty=True)

            if self.config.caddyfile_exists:
                self.output.info("Setting up Caddy web server...")
                _, result_ok = conn.run("command -v caddy", warn=True, hide=True)
                if result_ok:
                    self.output.warning("Caddy is already installed.")
                    self.output.output(
                        "Please ensure your Caddyfile includes the following line to load Fujin configurations:"
                    )
                    self.output.output("[bold]import conf.d/*.caddy[/bold]")
                else:
                    version = caddy.get_latest_gh_tag()
                    self.output.info(f"Installing Caddy version {version}...")
                    commands = caddy.get_install_commands(version)
                    conn.run(" && ".join(commands), pty=True)

            self.output.success("Server bootstrap completed successfully!")
            self.output.warning("Log out and back in for group changes to take effect")

    @cappa.command(
        help="Upgrade server components (Caddy, uv, Python, system packages)"
    )
    def upgrade(self):
        with self.connection() as conn:
            self.output.info("Upgrading server components...")

            all_success = True

            self._upgrade_system(conn)

            _, caddy_installed = conn.run("command -v caddy", warn=True, hide=True)

            if caddy_installed:

                def _caddy_version():
                    version, _ = conn.run("caddy version", hide=True, warn=True)
                    if not version:
                        return "unknown"
                    parts = version.strip().split()
                    return parts[0] if parts else "unknown"

                self.output.info("Upgrading Caddy web server...")
                current_version = _caddy_version()
                _, success = conn.run("sudo caddy upgrade", warn=True)

                if success:
                    new_version = _caddy_version()
                    if current_version != new_version:
                        self.output.success(
                            f"Caddy upgraded from {current_version} to {new_version}!"
                        )
                    else:
                        self.output.success(
                            f"Caddy is already at the latest version ({current_version})"
                        )
                else:
                    self.output.warning("Warning: Failed to upgrade Caddy.")
                    all_success = False
            else:
                self.output.info("Caddy is not installed, skipping upgrade.")

            _, uv_installed = conn.run("command -v uv", warn=True, hide=True)
            if uv_installed:

                def _uv_version():
                    version, _ = conn.run("uv --version", hide=True, warn=True)
                    if not version:
                        return "unknown"
                    parts = version.strip().split()
                    return (
                        parts[1] if len(parts) > 1 else parts[0] if parts else "unknown"
                    )

                self.output.info("Upgrading uv package manager...")
                current_version = _uv_version()
                _, success = conn.run("uv self update", warn=True)
                if success:
                    new_version = _uv_version()
                    if current_version != new_version:
                        self.output.success(
                            f"uv upgraded from {current_version} to {new_version}!"
                        )
                    else:
                        self.output.success(
                            f"uv is already at the latest version ({current_version})"
                        )
                else:
                    self.output.warning("Warning: Failed to upgrade uv.")
                    all_success = False
            else:
                self.output.warning("uv is not installed, skipping upgrade.")

            if uv_installed:
                self.output.info("Upgrading Python installations...")
                _, success = conn.run(
                    "uv python upgrade --preview-features python-upgrade", warn=True
                )
                if success:
                    self.output.success("Python installations upgraded successfully!")
                else:
                    self.output.warning(
                        "Warning: Failed to upgrade Python installations."
                    )
                    all_success = False
            else:
                self.output.warning("uv is not installed, skipping Python upgrade.")

            if all_success:
                self.output.success("All server components upgraded successfully!")
            else:
                self.output.warning(
                    "Server upgrade completed with some warnings. Check messages above."
                )

    def _upgrade_system(self, conn, packages: str | None = None):
        self.output.info("Upgrading system packages...")
        apt_opts = (
            "-o Dpkg::Options::='--force-confdef' -o Dpkg::Options::='--force-confold'"
        )
        command = f"sudo apt update -qq && sudo DEBIAN_FRONTEND=noninteractive apt upgrade -y -qq {apt_opts}"
        if packages:
            command += f"&& sudo apt install -y -qq {apt_opts} {packages}"
        _, success = conn.run(command, warn=True)
        if success:
            self.output.success("System packages upgraded successfully!")
        else:
            self.output.warning("Warning: Failed to upgrade system packages.")

    @cappa.command(
        name="create-user", help="Create a new user with sudo and ssh access"
    )
    def create_user(
        self,
        name: str,
        with_password: Annotated[
            bool, cappa.Arg(long="--with-password")
        ] = False,  # no short arg to force explicitness
    ):
        with self.connection() as conn:
            commands = [
                f"sudo adduser --disabled-password --gecos '' {name}",
                f"sudo mkdir -p /home/{name}/.ssh",
                f"sudo cp ~/.ssh/authorized_keys /home/{name}/.ssh/",
                f"sudo chown -R {name}:{name} /home/{name}/.ssh",
            ]
            if with_password:
                password = secrets.token_hex(8)
                commands.append(f"echo '{name}:{password}' | sudo chpasswd")
                self.output.success(f"Generated password: {password}")
            commands.extend(
                [
                    f"sudo chmod 700 /home/{name}/.ssh",
                    f"sudo chmod 600 /home/{name}/.ssh/authorized_keys",
                    f"echo '{name} ALL=(ALL) NOPASSWD:ALL' | sudo tee -a /etc/sudoers",
                ]
            )
            conn.run(" && ".join(commands), pty=True)
            self.output.success(f"New user {name} created successfully!")

    @cappa.command(
        name="setup-ssh", help="Interactive SSH key setup and fujin.toml configuration"
    )
    def setup_ssh(self):
        """Set up SSH key authentication and update fujin.toml."""
        self.output.info("SSH Setup Helper")
        self.output.output("")

        # Prompt for server details
        try:
            ip = Prompt.ask("Enter server IP or hostname")
            username = Prompt.ask("Enter username", default="root")
            password = Prompt.ask(
                "Enter password (or press Enter to use existing SSH key)",
                password=True,
                default="",
            )
        except KeyboardInterrupt:
            raise cappa.Exit("Setup cancelled", code=0)

        # Check for existing SSH key
        ssh_dir = Path.home() / ".ssh"
        ssh_dir.mkdir(mode=0o700, exist_ok=True)

        # Check for common key types
        key_paths = [
            ssh_dir / "id_ed25519",
            ssh_dir / "id_rsa",
        ]

        existing_key = None
        for key_path in key_paths:
            if key_path.exists():
                existing_key = key_path
                break

        if not existing_key:
            self.output.info("No SSH key found. Generating new ed25519 key...")
            key_path = ssh_dir / "id_ed25519"
            try:
                subprocess.run(
                    ["ssh-keygen", "-t", "ed25519", "-f", str(key_path), "-N", ""],
                    check=True,
                    capture_output=True,
                )
                self.output.success(f"Generated SSH key: {key_path}")
                existing_key = key_path
            except subprocess.CalledProcessError as e:
                raise SSHKeyError(f"Failed to generate SSH key: {e}") from e
        else:
            self.output.info(f"Using existing SSH key: {existing_key}")

        self.output.info(f"Copying SSH key to {username}@{ip}...")

        ssh_copy_cmd = ["ssh-copy-id", "-i", str(existing_key)]
        if password:
            sshpass_available = bool(shutil.which("sshpass"))
            if sshpass_available:
                ssh_copy_cmd = ["sshpass", "-p", password] + ssh_copy_cmd
            else:
                self.output.warning(
                    "sshpass not found. You'll need to enter the password manually."
                )

        ssh_copy_cmd.append(f"{username}@{ip}")

        try:
            result = subprocess.run(ssh_copy_cmd, capture_output=False)
            if result.returncode != 0:
                raise SSHKeyError("Failed to copy SSH key to server")
            self.output.success("SSH key copied to server successfully!")
        except FileNotFoundError as e:
            raise SSHKeyError(
                "ssh-copy-id not found. Please install OpenSSH client."
            ) from e

        fujin_toml = Path("fujin.toml")

        if fujin_toml.exists():
            self.output.info("Updating existing fujin.toml...")
            config_data = tomllib.loads(fujin_toml.read_text())
        else:
            self.output.info("Creating new fujin.toml...")
            config_data = {}

        # Override first host configuration
        hosts = config_data.get("hosts", [])
        if len(hosts) == 0:
            hosts = [{}]
        hosts[0] = {"address": ip, "user": username}
        config_data["hosts"] = hosts

        fujin_toml.write_text(tomli_w.dumps(config_data, multiline_strings=True))
        self.output.success(f"Updated fujin.toml with connection details!")

        self.output.output("")
        self.output.success("SSH setup completed successfully!")
        self.output.info(f"You can now connect to {username}@{ip} without a password.")

    @cappa.command(help="Execute command on the server")
    def exec(
        self,
        command: Annotated[str, cappa.Arg(help="Command to execute")],
        appenv: Annotated[
            bool,
            cappa.Arg(
                long="--appenv",
                help="Change to app directory and enable app environment",
            ),
        ] = False,
    ):
        with self.connection() as conn:
            if not appenv:
                return conn.run(command, pty=True)
            cmd = f"cd {self.config.app_dir} && source .install/.appenv && {command}"
            conn.run(
                f"sudo -u {self.config.app_user} bash -c {shlex.quote(cmd)}",
                pty=True,
            )
