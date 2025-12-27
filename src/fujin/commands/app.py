from __future__ import annotations

import shlex
import subprocess
import json
from typing import Annotated
from datetime import datetime
from collections import defaultdict

import cappa
from rich.table import Table
from rich.console import Console
from rich.markup import escape

from fujin.commands import BaseCommand
from fujin.config import InstallationMode


@cappa.command(
    help="Manage your application",
)
class App(BaseCommand):
    @cappa.command(help="Display application information and process status")
    def info(self):
        with self.connection() as conn:
            app_dir = shlex.quote(self.config.app_dir(self.selected_host))
            names = self.config.systemd_units
            delimiter = "___FUJIN_DELIM___"

            # Combine commands to reduce SSH roundtrips
            # 1. Get remote version from .version file
            # 2. List files in .versions directory for rollback targets
            # 3. Get service statuses (systemctl)
            cmds = [
                f"cat {app_dir}/.version 2>/dev/null || true",
                f"ls -1t {app_dir}/.versions 2>/dev/null || true",
                f"sudo systemctl is-active {' '.join(names)}",
            ]
            full_cmd = f"; echo '{delimiter}'; ".join(cmds)
            result_stdout, _ = conn.run(full_cmd, warn=True, hide=True)
            parts = result_stdout.split(delimiter)
            remote_version = parts[0].strip() or "N/A"

            # Parse rollback targets from filenames
            rollback_files = parts[1].strip().splitlines()
            rollback_versions = []
            prefix = f"{self.config.app_name}-"
            suffix = ".pyz"
            for fname in rollback_files:
                fname = fname.strip()
                if fname.startswith(prefix) and fname.endswith(suffix):
                    v = fname[len(prefix) : -len(suffix)]
                    if v != remote_version:
                        rollback_versions.append(v)

            rollback_targets = (
                ", ".join(rollback_versions) if rollback_versions else "N/A"
            )

            infos = {
                "app_name": self.config.app_name,
                "app_dir": self.config.app_dir(self.selected_host),
                "app_bin": self.config.app_bin,
                "local_version": self.config.version,
                "remote_version": remote_version,
                "rollback_targets": (
                    ", ".join(rollback_targets.split("\n"))
                    if rollback_targets
                    else "N/A"
                ),
            }
            if self.config.installation_mode == InstallationMode.PY_PACKAGE:
                infos["python_version"] = self.config.python_version

            if self.config.webserver.enabled:
                infos["running_at"] = f"https://{self.selected_host.domain_name}"

            services_status = {}
            statuses = parts[2].strip().split("\n")
            services_status = dict(zip(names, statuses))

            services = {}
            for process_name in self.config.processes:
                unit_names = self.config.get_unit_names(process_name)
                running_count = sum(
                    1 for name in unit_names if services_status.get(name) == "active"
                )
                total_count = len(unit_names)

                if total_count == 1:
                    services[process_name] = services_status.get(
                        unit_names[0], "unknown"
                    )
                else:
                    services[process_name] = f"{running_count}/{total_count}"

            socket_name = f"{self.config.app_name}.socket"
            if socket_name in services_status:
                services["socket"] = services_status[socket_name]

        # Format info text with clickable URL
        info_lines = [f"{key}: {value}" for key, value in infos.items()]
        infos_text = "\n".join(info_lines)

        table = Table(title="", header_style="bold cyan")
        table.add_column("Process", style="")
        table.add_column("Status")
        for service, status in services.items():
            if status == "active":
                status_str = f"[bold green]{status}[/bold green]"
            elif status == "failed":
                status_str = f"[bold red]{status}[/bold red]"
            elif status in ("inactive", "unknown"):
                status_str = f"[dim]{status}[/dim]"
            elif "/" in status:
                running, total = map(int, status.split("/"))
                if running == total:
                    status_str = f"[bold green]{status}[/bold green]"
                elif running == 0:
                    status_str = f"[bold red]{status}[/bold red]"
                else:
                    status_str = f"[bold yellow]{status}[/bold yellow]"
            else:
                status_str = status

            table.add_row(service, status_str)

        self.output.output(infos_text)
        self.output.output(table)

    @cappa.command(
        help="Start an interactive shell session using the system SSH client"
    )
    def shell(
        self,
        command: Annotated[
            str,
            cappa.Arg(
                help="Optional command to run. If not provided, starts a default shell"
            ),
        ] = "$SHELL",
    ):
        host = self.selected_host
        ssh_target = f"{host.user}@{host.ip or host.domain_name}"
        ssh_cmd = ["ssh", "-t"]
        if host.ssh_port:
            ssh_cmd.extend(["-p", str(host.ssh_port)])
        if host.key_filename:
            ssh_cmd.extend(["-i", str(host.key_filename)])

        full_remote_cmd = f"cd {self.config.app_dir(self.selected_host)} && source .appenv && {command}"
        ssh_cmd.extend([ssh_target, full_remote_cmd])
        subprocess.run(ssh_cmd)

    @cappa.command(
        help="Start the specified service or all services if no name is provided"
    )
    def start(
        self,
        name: Annotated[
            str | None, cappa.Arg(help="Service name, no value means all")
        ] = None,
    ):
        self._run_service_command("start", name)

    @cappa.command(
        help="Restart the specified service or all services if no name is provided"
    )
    def restart(
        self,
        name: Annotated[
            str | None, cappa.Arg(help="Service name, no value means all")
        ] = None,
    ):
        self._run_service_command("restart", name)

    @cappa.command(
        help="Stop the specified service or all services if no name is provided"
    )
    def stop(
        self,
        name: Annotated[
            str | None, cappa.Arg(help="Service name, no value means all")
        ] = None,
    ):
        self._run_service_command("stop", name)

    def _run_service_command(self, command: str, name: str | None):
        with self.connection() as conn:
            # Use instances for start/stop/restart (operates on running services)
            names = self._resolve_units(name, use_templates=False)
            if not names:
                self.output.warning("No services found")
                return

            self.output.output(
                f"Running [cyan]{command}[/cyan] on: [cyan]{', '.join(names)}[/cyan]"
            )
            conn.run(f"sudo systemctl {command} {' '.join(names)}", pty=True)

        msg = f"{name} service" if name else "All Services"
        past_tense = {
            "start": "started",
            "restart": "restarted",
            "stop": "stopped",
        }.get(command, command)
        self.output.success(f"{msg} {past_tense} successfully!")

    @cappa.command(help="Show logs for the specified service")
    def logs(
        self,
        name: Annotated[str | None, cappa.Arg(help="Service name")] = None,
        follow: Annotated[
            bool, cappa.Arg(short="-f", long="--follow", help="Follow log output")
        ] = False,
        lines: Annotated[
            int,
            cappa.Arg(short="-n", long="--lines", help="Number of log lines to show"),
        ] = 50,
        level: Annotated[
            str | None,
            cappa.Arg(
                long="--level",
                help="Filter by log level",
                choices=[
                    "emerg",
                    "alert",
                    "crit",
                    "err",
                    "warning",
                    "notice",
                    "info",
                    "debug",
                ],
            ),
        ] = None,
        since: Annotated[
            str | None,
            cappa.Arg(
                long="--since",
                help="Show logs since specified time (e.g., '2 hours ago', '2024-01-01', 'yesterday')",
            ),
        ] = None,
        grep: Annotated[
            str | None,
            cappa.Arg(
                short="-g",
                long="--grep",
                help="Filter logs by pattern (case-insensitive)",
            ),
        ] = None,
    ):
        """
        # Show last 50 lines for web process (default)
        fujin app logs web

        # Follow logs in real-time
        fujin app logs web --follow

        # Show only error-level logs
        fujin app logs web --level err

        # Show logs from last 2 hours
        fujin app logs --since "2 hours ago"

        # Filter logs by pattern
        fujin app logs --grep "ValueError"

        # Combine multiple filters
        fujin app logs web -n 100 --level warning --since "1 hour ago"
        """
        with self.connection() as conn:
            # Use instances for logs (shows logs from running services)
            names = self._resolve_units(name, use_templates=False)

            if names:
                units = " ".join(f"-u {n}" for n in names)

                cmd_parts = ["sudo journalctl", units]
                if not follow:
                    cmd_parts.append(f"-n {lines}")
                if level:
                    cmd_parts.append(f"-p {level}")
                if since:
                    cmd_parts.append(f"--since {shlex.quote(since)}")
                if grep:
                    cmd_parts.append(f"-g {shlex.quote(grep)}")
                if follow:
                    cmd_parts.append("-f")

                journalctl_cmd = " ".join(cmd_parts)

                self.output.output(f"Showing logs for: [cyan]{', '.join(names)}[/cyan]")
                conn.run(journalctl_cmd, warn=True, pty=True)
            else:
                self.output.warning("No services found")

    @cappa.command(help="Show the systemd unit file content for the specified service")
    def cat(
        self,
        name: Annotated[str | None, cappa.Arg(help="Service name")] = None,
    ):
        if not name:
            self.output.info("Available options:")
            self.output.output(self._get_available_options())
            return

        with self.connection() as conn:
            if name == "caddy" and self.config.webserver.enabled:
                self.output.output(f"[cyan]# {self.config.caddy_config_path}[/cyan]")
                print()
                conn.run(f"cat {self.config.caddy_config_path}")
                print()
                return

            if name == "units":
                names = self.config.systemd_units
            else:
                names = self._resolve_units(name, use_templates=True)

            if not names:
                self.output.warning("No services found")
                return

            conn.run(f"sudo systemctl cat {' '.join(names)}", pty=True)

    @cappa.command(help="Show deployment history")
    def history(
        self,
        limit: Annotated[
            int,
            cappa.Arg(
                short="-n",
                long="--limit",
                help="Number of deployments to show",
            ),
        ] = 10,
    ):
        """Display deployment history from the server."""
        with self.connection() as conn:
            app_dir = shlex.quote(self.config.app_dir(self.selected_host))
            history_file = f"{app_dir}/.deployments.json"

            stdout, success = conn.run(
                f"cat {history_file} 2>/dev/null", hide=True, warn=True
            )

            if not success or not stdout.strip():
                self.output.warning("No deployment history found")
                return

            try:
                history = json.loads(stdout)
            except json.JSONDecodeError:
                self.output.error("Failed to parse deployment history")
                return

            if not history:
                self.output.warning("No deployments recorded")
                return

            history = history[:limit]

            grouped: dict[str, list[dict]] = defaultdict(list)
            for record in history:
                host = record.get("host", "unknown")
                grouped[host].append(record)

            console = Console()

            first = True
            for host, host_records in grouped.items():
                if not first:
                    console.print()
                console.print(f"[green]{host}[/green]:")
                first = False

                for record in host_records:
                    try:
                        ts = datetime.fromisoformat(record["timestamp"])
                        timestamp = ts.strftime("%Y-%m-%d %H:%M")
                    except (ValueError, KeyError):
                        timestamp = record.get("timestamp", "unknown")

                    user = record.get("user", "unknown")
                    version = record.get("version", "unknown")
                    git_commit = record.get("git_commit", "")

                    message = f"Deployed version [blue]{version}[/blue]"
                    if git_commit:
                        message += f" [dim]({git_commit[:7]})[/dim]"

                    console.print(
                        f"  [{escape(timestamp)}] [dim]\\[[/dim][yellow]{user}[/yellow][dim]][/dim] {message}",
                        highlight=False,
                    )
