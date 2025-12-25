from __future__ import annotations

import secrets
import time
from typing import Annotated

import cappa
from rich.live import Live
from rich.table import Table

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
      fujin server stats          Display resource usage (CPU, memory, disk, network)
      fujin server stats --watch  Continuously monitor resources
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

    @cappa.command(help="Display server resource usage statistics")
    def stats(
        self,
        watch: Annotated[
            bool,
            cappa.Arg(
                short="-w",
                long="--watch",
                help="Continuously monitor resources (updates every 2 seconds)",
            ),
        ] = False,
        interval: Annotated[
            int,
            cappa.Arg(
                short="-i",
                long="--interval",
                help="Update interval in seconds for watch mode",
            ),
        ] = 2,
    ):
        with self.connection() as conn:
            if watch:
                try:
                    with Live(
                        self._get_stats_display(conn), refresh_per_second=4
                    ) as live:
                        while True:
                            time.sleep(interval)
                            live.update(self._get_stats_display(conn))
                except KeyboardInterrupt:
                    self.output.info("\nStopped monitoring.")
            else:
                self.output.output(self._get_stats_display(conn))

    def _get_stats_display(self, conn):
        """Fetch and return server resource statistics as a Rich renderable."""
        # Build a combined command to fetch all stats in one SSH roundtrip
        delimiter = "___FUJIN_STATS_DELIM___"
        commands = [
            # CPU usage from top
            "top -bn1 | grep 'Cpu(s)' | awk '{print $2}' | cut -d'%' -f1",
            # Memory stats from free (in MB)
            "free -m | awk 'NR==2{printf \"%s %s %.1f\", $3,$2,($3/$2)*100}'",
            # Disk usage for root partition
            "df -h / | awk 'NR==2{printf \"%s %s %s\", $3,$2,$5}'",
            # Network stats (first active interface)
            "cat /proc/net/dev | awk 'NR>2 && $2>0 {print $1,$2,$10; exit}' | sed 's/://g'",
        ]
        full_cmd = f"; echo '{delimiter}'; ".join(commands)
        result, _ = conn.run(full_cmd, warn=True, hide=True)

        parts = result.split(delimiter)

        # Parse results
        cpu_usage = parts[0].strip() if len(parts) > 0 else "N/A"
        memory_parts = (
            parts[1].strip().split() if len(parts) > 1 else ["N/A", "N/A", "N/A"]
        )
        disk_parts = (
            parts[2].strip().split() if len(parts) > 2 else ["N/A", "N/A", "N/A"]
        )
        network_parts = (
            parts[3].strip().split() if len(parts) > 3 else ["N/A", "0", "0"]
        )

        # Build table
        table = Table(
            title="Server Resource Usage", show_header=False, box=None, padding=(0, 2)
        )
        table.add_column("Metric", style="bold")
        table.add_column("Value")

        # CPU
        cpu_color = (
            "green"
            if cpu_usage != "N/A" and float(cpu_usage) < 70
            else "yellow"
            if cpu_usage != "N/A" and float(cpu_usage) < 90
            else "red"
        )
        table.add_row("CPU", f"[{cpu_color}]{cpu_usage}%[/{cpu_color}]")

        # Memory
        if len(memory_parts) >= 3:
            mem_used, mem_total, mem_percent = (
                memory_parts[0],
                memory_parts[1],
                memory_parts[2],
            )
            mem_color = (
                "green"
                if float(mem_percent) < 70
                else "yellow"
                if float(mem_percent) < 90
                else "red"
            )
            table.add_row(
                "Memory",
                f"[{mem_color}]{mem_used} MB / {mem_total} MB ({mem_percent}%)[/{mem_color}]",
            )
        else:
            table.add_row("Memory", "N/A")

        # Disk
        if len(disk_parts) >= 3:
            disk_used, disk_total, disk_percent = (
                disk_parts[0],
                disk_parts[1],
                disk_parts[2],
            )
            disk_color = (
                "green"
                if disk_percent.replace("%", "") != "N/A"
                and int(disk_percent.replace("%", "")) < 70
                else "yellow"
                if disk_percent.replace("%", "") != "N/A"
                and int(disk_percent.replace("%", "")) < 90
                else "red"
            )
            table.add_row(
                "Disk (/)",
                f"[{disk_color}]{disk_used} / {disk_total} ({disk_percent})[/{disk_color}]",
            )
        else:
            table.add_row("Disk (/)", "N/A")

        # Network
        if len(network_parts) >= 3 and network_parts[0] != "N/A":
            iface, rx_bytes, tx_bytes = (
                network_parts[0],
                int(network_parts[1]),
                int(network_parts[2]),
            )
            rx_mb = rx_bytes / (1024 * 1024)
            tx_mb = tx_bytes / (1024 * 1024)
            table.add_row(
                "Network", f"{iface}: ↓ {rx_mb:.1f} MB  ↑ {tx_mb:.1f} MB (total)"
            )
        else:
            table.add_row("Network", "N/A")

        return table
