from __future__ import annotations

import cappa
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from fujin.commands import BaseCommand


@cappa.command(name="config", help="Display your current configuration")
class ConfigCMD(BaseCommand):
    def __call__(self):
        console = Console()

        # General Configuration
        general_table = Table(show_header=False, box=None, expand=True)
        general_table.add_column("Key", style="bold green")
        general_table.add_column("Value")

        general_data = {
            "App Name": self.config.app_name,
            "App Binary": self.config.app_bin,
            "Version": self.config.version,
            "Build Command": self.config.build_command,
            "Release Command": self.config.release_command or "N/A",
            "Installation Mode": self.config.installation_mode,
            "Distfile": self.config.distfile,
            "Webserver Upstream": self.config.webserver.upstream,
            "Webserver Enabled": str(self.config.webserver.enabled),
        }
        if self.config.python_version:
            general_data["Python Version"] = self.config.python_version

        for key, value in general_data.items():
            general_table.add_row(key, str(value))

        console.print(
            Panel(
                general_table,
                title="General Configuration",
                border_style="green",
                expand=False,
            )
        )

        # Host Configuration
        host_table = Table(show_header=False, box=None, expand=True)
        host_table.add_column("Key", style="dim")
        host_table.add_column("Value")

        host_data = {
            "Domain": self.config.host.domain_name,
            "User": self.config.host.user,
            "IP": self.config.host.ip,
            "Apps Dir": self.config.host.apps_dir,
            "SSH Port": str(self.config.host.ssh_port),
        }
        if self.config.host.key_filename:
            host_data["Key Filename"] = str(self.config.host.key_filename)

        if self.config.host.env_content:
            host_data["Env Content"] = (
                self.config.host.env_content[:50] + "..."
                if len(self.config.host.env_content) > 50
                else self.config.host.env_content
            )

        for key, value in host_data.items():
            host_table.add_row(key, str(value))

        console.print(
            Panel(
                host_table,
                title="Host Configuration",
                expand=False,
            )
        )

        # Processes Table
        processes_table = Table(
            title="Processes", header_style="bold cyan", box=box.SIMPLE, expand=True
        )
        processes_table.add_column("Name", style="bold")
        processes_table.add_column("Command")
        processes_table.add_column("Replicas", justify="center")
        processes_table.add_column("Socket", justify="center")

        for name, config in self.config.processes.items():
            command = config.command
            replicas = config.replicas
            socket = config.socket

            processes_table.add_row(
                name,
                command,
                str(replicas),
                "[green]Yes[/green]" if socket else "[dim]No[/dim]",
            )
        console.print(processes_table)

        # Aliases Table
        if self.config.aliases:
            aliases_table = Table(
                title="Aliases", header_style="bold cyan", box=box.SIMPLE, expand=True
            )
            aliases_table.add_column("Alias", style="bold")
            aliases_table.add_column("Command")
            for alias, command in self.config.aliases.items():
                aliases_table.add_row(alias, command)

            console.print(aliases_table)
