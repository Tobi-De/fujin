from __future__ import annotations

import secrets
from typing import Annotated

import cappa

from fujin import caddy
from fujin.commands import BaseCommand


@cappa.command(
    help="Manage server operations",
)
class Server(BaseCommand):
    """
    Examples:
      fujin server bootstrap      Setup server with dependencies and Caddy
      fujin server info           Show server system information
      fujin server exec ls        Run command on server
    """

    @cappa.command(help="Display information about the host system")
    def info(self):
        with self.connection() as conn:
            _, result_ok = conn.run(f"command -v fastfetch", warn=True, hide=True)
            if result_ok:
                conn.run("fastfetch", pty=True)
            else:
                self.output.output(conn.run("cat /etc/os-release", hide=True)[0])

    @cappa.command(help="Setup uv, web proxy, and install necessary dependencies")
    def bootstrap(self):
        with self.connection() as conn:
            self.output.info("Bootstrapping server...")
            _, server_update_ok = conn.run(
                "sudo apt update && sudo DEBIAN_FRONTEND=noninteractive apt upgrade -y && sudo apt install -y sqlite3 curl rsync",
                pty=True,
                warn=True,
            )
            if not server_update_ok:
                self.output.warning(
                    "Warning: Failed to update and upgrade the server packages."
                )
            _, result_ok = conn.run("command -v uv", warn=True)
            if not result_ok:
                self.output.info("Installing uv tool...")
                conn.run(
                    "curl -LsSf https://astral.sh/uv/install.sh | sh && uv tool update-shell"
                )
            conn.run("uv tool install fastfetch-bin-edge")
            if self.config.webserver.enabled:
                self.output.info("Setting up Caddy web server...")

                _, result_ok = conn.run(f"command -v caddy", warn=True, hide=True)
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

    @cappa.command(help="Execute an arbitrary command on the server")
    def exec(
        self,
        command: str,
        appenv: Annotated[
            bool,
            cappa.Arg(
                default=False,
                long="--appenv",
                help="Change to app directory and enable app environment",
            ),
        ],
    ):
        with self.connection() as conn:
            command = (
                f"cd {self.config.app_dir} && source .appenv && {command}"
                if appenv
                else command
            )
            conn.run(command, pty=True)

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
