from __future__ import annotations

import secrets
from functools import partial
from typing import Annotated

import cappa

from fujin import caddy
from fujin.commands import BaseCommand


@cappa.command(help="Manage server operations")
class Server(BaseCommand):
    @cappa.command(help="Display information about the host system")
    def info(self):
        with self.connection() as conn:
            _, result_ok = conn.run(f"command -v fastfetch", warn=True, hide=True)
            if result_ok:
                conn.run("fastfetch", pty=True)
            else:
                self.stdout.output(conn.run("cat /etc/os-release", hide=True)[0])

    @cappa.command(help="Setup uv, web proxy, and install necessary dependencies")
    def bootstrap(self):
        with self.connection() as conn:
            self.stdout.output("[blue]Bootstrapping server...[/blue]")
            conn.run("sudo apt update && sudo apt upgrade -y", pty=True)
            conn.run("sudo apt install -y sqlite3 curl rsync", pty=True)
            _, result_ok = conn.run("command -v uv", warn=True)
            if not result_ok:
                self.output.output("[blue]Installing uv tool...[/blue]")
                conn.run("curl -LsSf https://astral.sh/uv/install.sh | sh")
                conn.run("uv tool update-shell")
            conn.run("uv tool install fastfetch-bin-edge")
            if self.config.webserver.enabled:
                self.stdout.output("[blue]Setting up Caddy web server...[/blue]")
                installed = caddy.install(conn)
                if not installed:
                    self.stdout.output("[yellow]Caddy is already installed.[/yellow]")
                    self.stdout.output(
                        "Please ensure your Caddyfile includes the following line to load Fujin configurations:"
                    )
                    self.stdout.output("[bold]import conf.d/*.caddy[/bold]")
            self.stdout.output(
                "[green]Server bootstrap completed successfully![/green]"
            )

    @cappa.command(
        help="Execute an arbitrary command on the server, only for non-interactive commands"
    )
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
            run_pty = partial(conn.run, pty=True)
            run_pty(
                f"sudo adduser --disabled-password --gecos '' {name}",
            )
            run_pty(f"sudo mkdir -p /home/{name}/.ssh")
            run_pty(f"sudo cp ~/.ssh/authorized_keys /home/{name}/.ssh/")
            run_pty(f"sudo chown -R {name}:{name} /home/{name}/.ssh")
            if with_password:
                password = secrets.token_hex(8)
                run_pty(f"echo '{name}:{password}' | sudo chpasswd")
                self.stdout.output(f"[green]Generated password: [/green]{password}")
            run_pty(f"sudo chmod 700 /home/{name}/.ssh")
            run_pty(f"sudo chmod 600 /home/{name}/.ssh/authorized_keys")
            run_pty(f"echo '{name} ALL=(ALL) NOPASSWD:ALL' | sudo tee -a /etc/sudoers")
            self.stdout.output(f"[green]New user {name} created successfully![/green]")
