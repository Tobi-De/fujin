from __future__ import annotations

import secrets
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Annotated

import cappa
import tomli_w
from rich.prompt import Confirm, Prompt
from rich.table import Table

from fujin import caddy
from fujin import serverops
from fujin import sudoers
from fujin.commands import BaseCommand
from fujin.config import tomllib
from fujin import connection
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
      fujin server harden         Replace NOPASSWD:ALL with a scoped rule
      fujin server status         Show server system information
    """

    @cappa.command(help="Display information about the host system")
    def status(self):
        with connection.connection(host=self.selected_host) as conn:
            _, result_ok = conn.run("command -v fastfetch", warn=True, hide=True)
            if result_ok:
                conn.run("fastfetch", pty=True)
            else:
                self.output.output(conn.run("cat /etc/os-release", hide=True)[0])

    @cappa.command(help="Setup uv, web proxy, and install necessary dependencies")
    def bootstrap(self):
        with connection.connection(host=self.selected_host) as conn:
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

            # Set up app user, group, and directory
            app_user = self.config.app_user
            app_dir = self.config.app_dir
            self.output.info(f"Setting up {app_user} user...")
            conn.run(
                f"sudo groupadd -f {app_user} && "
                f"sudo useradd --system --no-create-home --shell /usr/sbin/nologin "
                f"-g {app_user} {app_user} || true",
                pty=True,
            )
            self.output.info(f"Setting up {app_dir} directory...")
            conn.run(f"sudo mkdir -p {app_dir}", pty=True)
            conn.run(f"sudo mkdir -p {app_dir}/shared", pty=True)
            conn.run(
                f"sudo chown -R {self.selected_host.user}:{app_user} {app_dir}",
                pty=True,
            )
            conn.run(f"sudo chmod 775 {app_dir}", pty=True)

            self.output.info(
                "Setting up release-tree layout (releases, store, shared)..."
            )
            serverops.ensure_layout(conn, app_dir, app_user, self.selected_host.user)

            # Add caddy to app group and restart to pick up group membership
            self.output.info(f"Adding caddy to {app_user} group...")
            conn.run(f"sudo usermod -aG {app_user} caddy", warn=True)
            # Only restart caddy if it is actually installed; a fresh box gets
            # Caddy further below, after the release-tree layout exists.
            _, caddy_present = conn.run("command -v caddy", warn=True, hide=True)
            if caddy_present:
                conn.run("sudo systemctl restart caddy", warn=True)

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
        with connection.connection(host=self.selected_host) as conn:
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
        # DEBIAN_FRONTEND survives the sudo call because the scoped rule has
        # `Defaults:<user> env_keep += "DEBIAN_FRONTEND"`.  The Dpkg options
        # are part of the exact command lines allowed by the rule.
        apt_opts = (
            "-o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold"
        )
        command = (
            "DEBIAN_FRONTEND=noninteractive sudo apt-get update -qq && "
            f"DEBIAN_FRONTEND=noninteractive sudo apt-get -y -qq {apt_opts} upgrade"
        )
        if packages:
            command += (
                " && DEBIAN_FRONTEND=noninteractive "
                f"sudo apt-get -y -qq {apt_opts} install {packages}"
            )
        _, success = conn.run(command, warn=True)
        if success:
            self.output.success("System packages upgraded successfully!")
        else:
            self.output.warning("Warning: Failed to upgrade system packages.")

    @cappa.command(
        name="create-user",
        help="Create a new user with a scoped sudoers rule and ssh access",
    )
    def create_user(
        self,
        name: str,
        with_password: Annotated[
            bool, cappa.Arg(long="--with-password")
        ] = False,  # no short arg to force explicitness
    ):
        try:
            rule = sudoers.render_rule(name, self.config.app_user, self.config.app_name)
        except ValueError as exc:
            raise cappa.Exit(str(exc), code=1) from None

        with connection.connection(host=self.selected_host) as conn:
            # Refuse to create a user whose rule cannot take effect: without
            # the include the drop-in is dead weight (and a later harden
            # would have to abort).  The same check runs again inside the
            # root script, where it is authoritative.
            content, readable = conn.run(
                f"sudo cat {shlex.quote(sudoers.SUDOERS_PATH)}", warn=True, hide=True
            )
            if not readable:
                raise cappa.Exit(
                    f"Could not read {sudoers.SUDOERS_PATH}; cannot verify that "
                    f"it loads {sudoers.DROPIN_DIR}. Aborting without changes.",
                    code=1,
                )
            if not sudoers.has_includedir(content):
                raise cappa.Exit(
                    f"{sudoers.SUDOERS_PATH} does not include {sudoers.DROPIN_DIR}. "
                    "Add a line such as `#includedir /etc/sudoers.d` to "
                    f"{sudoers.SUDOERS_PATH} (as root) and re-run create-user.",
                    code=1,
                )

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
                ]
            )
            conn.run(" && ".join(commands), pty=True)

            self.output.info("Installing scoped sudoers rule...")
            script = sudoers.install_dropin_script(name, rule)
            output, installed = conn.run_script(script, sudo=True, warn=True)
            if not installed or sudoers.DROPIN_OK not in output:
                raise cappa.Exit(
                    f"User {name} was created, but the sudoers rule was not "
                    "installed (visudo validation failed or the include is "
                    f"missing). The root script restored the previous state. "
                    "Fix the reported error and run `fujin server harden` "
                    "(or re-run create-user).",
                    code=1,
                )

            self.output.success(f"New user {name} created successfully!")
            self.output.info(f"Sudo rule installed at {sudoers.DROPIN_PATH(name)}")

    @cappa.command(
        name="harden",
        help="Replace NOPASSWD:ALL with a scoped sudoers rule (opt-in)",
    )
    def harden(
        self,
        dry_run: Annotated[
            bool,
            cappa.Arg(
                long="--dry-run",
                help=(
                    "Show the rule and the /etc/sudoers edit without changing anything"
                ),
            ),
        ] = False,
    ):
        """Migrate an existing server from ``NOPASSWD:ALL`` to the scoped rule.

        This is an **administrator action**: it runs as one ``sudo bash``
        script, so the connecting user needs full sudo (the scoped rule does
        not grant ``bash``/``visudo``).  The script backs up and restores
        everything it touches on any failure (see ``fujin.sudoers``).
        """
        user = self.selected_host.user
        app_user = self.config.app_user
        try:
            rule = sudoers.render_rule(user, app_user, self.config.app_name)
        except ValueError as exc:
            raise cappa.Exit(str(exc), code=1) from None

        dropin = sudoers.DROPIN_PATH(user)
        with connection.connection(host=self.selected_host) as conn:
            if dry_run:
                # The only read: dry-run inspects the current file without
                # changing anything.  The real migration does not race a
                # client-side copy: it derives the edit inside the script.
                content, ok = conn.run(
                    f"sudo cat {shlex.quote(sudoers.SUDOERS_PATH)}",
                    warn=True,
                    hide=True,
                )
                if not ok:
                    raise cappa.Exit(
                        f"Could not read {sudoers.SUDOERS_PATH} to plan the change.",
                        code=1,
                    )
                if not sudoers.has_includedir(content):
                    raise cappa.Exit(
                        f"{sudoers.SUDOERS_PATH} does not load "
                        f"{sudoers.DROPIN_DIR}; installing the scoped rule "
                        "would be ignored. Add a line such as "
                        f"`#includedir /etc/sudoers.d` to "
                        f"{sudoers.SUDOERS_PATH} and re-run harden.",
                        code=1,
                    )
                _, had_old_rule = sudoers.strip_nopasswd_all(content, user)
                self.output.info(f"Would install {dropin}:")
                self.output.output(rule)
                if had_old_rule:
                    self.output.info(
                        f"Would remove this line from {sudoers.SUDOERS_PATH}:"
                    )
                    self.output.output(f"{user} ALL=(ALL) NOPASSWD:ALL")
                else:
                    self.output.info(
                        f"No `{user} ALL=(ALL) NOPASSWD:ALL` line found; "
                        f"{sudoers.SUDOERS_PATH} would not be rewritten."
                    )
                self.output.info("Dry run: no changes made.")
                return

            # One root shell, one transaction, one round trip.  The script
            # re-derives the legacy line from the live file (no TOCTOU) and
            # restores both files itself if anything fails.
            script = sudoers.harden_script(user, app_user, self.config.app_name, rule)
            output, ok = conn.run_script(script, sudo=True, warn=True)
            if not ok or sudoers.HARDEN_OK not in output:
                reason = "unknown cause"
                for line in output.splitlines():
                    if sudoers.HARDEN_FAILED in line:
                        reason = line.split("reason=", 1)[-1].strip() or reason
                raise cappa.Exit(
                    f"harden failed ({reason}); the transaction was rolled back "
                    f"({sudoers.SUDOERS_PATH} and {dropin} were restored). "
                    "Inspect the command output above and fix the reported "
                    "cause before retrying.",
                    code=1,
                )

            backup = "none"
            for line in output.splitlines():
                if line.strip().startswith(sudoers.HARDEN_OK):
                    parts = line.split()
                    backup = next(
                        (p.split("=", 1)[1] for p in parts if p.startswith("backup=")),
                        "none",
                    )
            if backup != "none":
                self.output.success(
                    f"Removed `{user} ALL=(ALL) NOPASSWD:ALL` from "
                    f"{sudoers.SUDOERS_PATH}."
                )
                self.output.info(f"Backup of the previous sudoers file: {backup}")
            else:
                self.output.info(
                    f"No legacy NOPASSWD:ALL rule for {user}; "
                    f"{sudoers.SUDOERS_PATH} was not modified."
                )
            self.output.success(f"Installed scoped sudoers rule at {dropin}")

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
        self.output.success("Updated fujin.toml with connection details!")

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
        with connection.connection(host=self.selected_host) as conn:
            if not appenv:
                return conn.run(command, pty=True)
            cmd = f"cd {self.config.app_dir} && source current/.env && {command}"
            conn.run(
                f"sudo -u {self.config.app_user} bash -c {shlex.quote(cmd)}",
                pty=True,
            )

    @cappa.command(help="List authorized SSH keys")
    def keys(
        self,
        user: Annotated[
            str | None,
            cappa.Arg(long="--user", help="List keys for a specific user"),
        ] = None,
    ):
        with connection.connection(host=self.selected_host) as conn:
            cat = "sudo cat" if user else "cat"
            path = (
                f"/home/{user}/.ssh/authorized_keys"
                if user
                else "~/.ssh/authorized_keys"
            )
            content, success = conn.run(f"{cat} {path}", warn=True, hide=True)
            if not success or not content.strip():
                self.output.warning(
                    f"No authorized keys found for {user or self.selected_host.user}"
                )
                return

            table = Table(show_header=True)
            table.add_column("#", style="dim", width=3)
            table.add_column("Type", width=12)
            table.add_column("Fingerprint", width=48)
            table.add_column("Comment")

            for idx, line in enumerate(content.strip().splitlines(), 1):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split(None, 2)
                if len(parts) < 2:
                    continue
                fp_output, fp_ok = conn.run(
                    f"echo {shlex.quote(line)} | ssh-keygen -lf -", warn=True, hide=True
                )
                fingerprint = fp_output.split()[1] if fp_ok and fp_output else "unknown"
                table.add_row(
                    str(idx), parts[0], fingerprint, parts[2] if len(parts) > 2 else ""
                )

            self.output.output(table)

    @cappa.command(name="add-key", help="Generate SSH key pair and authorize it")
    def add_key(
        self,
        name: Annotated[
            str, cappa.Arg(help="Name/comment for the key (e.g., 'ci-deploy')")
        ],
        user: Annotated[
            str | None,
            cappa.Arg(long="--user", help="Add key for a specific user"),
        ] = None,
    ):
        with connection.connection(host=self.selected_host) as conn:
            comment = f"{name} (fujin@{date.today().isoformat()})"
            temp_key = f"/tmp/fujin_key_{secrets.token_hex(8)}"

            _, success = conn.run(
                f"ssh-keygen -t ed25519 -f {temp_key} -N '' -C {shlex.quote(comment)}",
                hide=True,
            )
            if not success:
                self.output.error("Failed to generate SSH key pair")
                return

            private_key, _ = conn.run(f"cat {temp_key}", hide=True)
            public_key, _ = conn.run(f"cat {temp_key}.pub", hide=True)

            if user:
                path = f"/home/{user}/.ssh"
                conn.run(
                    f"sudo mkdir -p {path} && "
                    f"echo {shlex.quote(public_key.strip())} | sudo tee -a {path}/authorized_keys && "
                    f"sudo chown -R {user}:{user} {path} && sudo chmod 600 {path}/authorized_keys",
                    hide=True,
                )
            else:
                conn.run(
                    f"mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
                    f"echo {shlex.quote(public_key.strip())} >> ~/.ssh/authorized_keys && "
                    f"chmod 600 ~/.ssh/authorized_keys",
                    hide=True,
                )

            conn.run(f"rm -f {temp_key} {temp_key}.pub", hide=True)

            self.output.success(f"Generated SSH key pair for: {name}")
            self.output.output("")
            self.output.output(
                "[bold]Private key (copy this to your CI secrets):[/bold]"
            )
            self.output.output("─" * 50)
            self.output.output(private_key.strip())
            self.output.output("─" * 50)
            self.output.output("")
            self.output.output(f"[bold]Public key added with comment:[/bold] {comment}")

    @cappa.command(name="remove-key", help="Remove an authorized SSH key")
    def remove_key(
        self,
        key: Annotated[
            str, cappa.Arg(help="Key index (from 'keys' command) or comment to match")
        ],
        user: Annotated[
            str | None,
            cappa.Arg(long="--user", help="Remove key from a specific user"),
        ] = None,
        force: Annotated[
            bool,
            cappa.Arg(long="--force", short="-f", help="Skip confirmation prompt"),
        ] = False,
    ):
        with connection.connection(host=self.selected_host) as conn:
            cat = "sudo cat" if user else "cat"
            path = (
                f"/home/{user}/.ssh/authorized_keys"
                if user
                else "~/.ssh/authorized_keys"
            )
            content, success = conn.run(f"{cat} {path}", warn=True, hide=True)
            if not success or not content.strip():
                self.output.warning(
                    f"No authorized keys found for {user or self.selected_host.user}"
                )
                return

            lines = content.strip().splitlines()
            key_lines = [
                (i, line)
                for i, line in enumerate(lines)
                if line.strip() and not line.strip().startswith("#")
            ]

            # Find the key to remove - try as index first, then as comment match
            remove_index, line_to_remove = None, None
            try:
                idx = int(key)
                if 1 <= idx <= len(key_lines):
                    remove_index, line_to_remove = key_lines[idx - 1]
            except ValueError:
                for orig_idx, line in key_lines:
                    if key in line:
                        remove_index, line_to_remove = orig_idx, line
                        break

            if line_to_remove is None:
                self.output.error(f"Key not found: {key}")
                return

            parts = line_to_remove.split(None, 2)
            comment = parts[2] if len(parts) > 2 else "(no comment)"
            self.output.output(f"Key to remove: [bold]{comment}[/bold]")

            if not force:
                try:
                    if not Confirm.ask("Are you sure?"):
                        return
                except KeyboardInterrupt:
                    return

            new_content = (
                "\n".join(l for i, l in enumerate(lines) if i != remove_index) + "\n"
            )
            write = f"sudo tee {path}" if user else f"tee {path}"
            conn.run(f"echo {shlex.quote(new_content)} | {write}", hide=True)
            self.output.success(f"Key removed: {comment}")
