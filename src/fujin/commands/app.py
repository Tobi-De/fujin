from __future__ import annotations

from typing import Annotated

import cappa
from rich.table import Table


from fujin.commands import BaseCommand
from fujin.config import InstallationMode


@cappa.command(help="Run application-related tasks")
class App(BaseCommand):
    @cappa.command(help="Display information about the application")
    def info(self):
        with self.connection() as conn:
            remote_version = (
                conn.run("head -n 1 .versions", warn=True, hide=True).stdout.strip()
                or "N/A"
            )
            rollback_targets = conn.run(
                "sed -n '2,$p' .versions", warn=True, hide=True
            ).stdout.strip()
            infos = {
                "app_name": self.config.app_name,
                "app_dir": self.config.app_dir,
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
                infos["running_at"] = f"https://{self.config.host.domain_name}"

            names = self.config.active_systemd_units
            if names:
                result = conn.run(
                    f"sudo systemctl is-active {' '.join(names)}",
                    warn=True,
                    hide=True,
                )
                statuses = result.stdout.strip().split("\n")
                services_status = dict(zip(names, statuses))
            else:
                services_status = {}

            services = {}
            for process_name in self.config.processes:
                active_systemd_units = self.config.get_active_unit_names(process_name)
                running_count = sum(
                    1
                    for name in active_systemd_units
                    if services_status.get(name) == "active"
                )
                total_count = len(active_systemd_units)

                if total_count == 1:
                    services[process_name] = services_status.get(
                        active_systemd_units[0], "unknown"
                    )
                else:
                    services[process_name] = f"{running_count}/{total_count}"

            socket_name = f"{self.config.app_name}.socket"
            if socket_name in services_status:
                services["socket"] = services_status[socket_name]

        infos_text = "\n".join(f"{key}: {value}" for key, value in infos.items())

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

        self.stdout.output(infos_text)
        self.stdout.output(table)

    @cappa.command(help="Run an arbitrary command via the application binary")
    def exec(
        self,
        command: str,
        interactive: Annotated[bool, cappa.Arg(default=False, short="-i")],
    ):
        with self.app_environment() as conn:
            if interactive:
                conn.run(f"{self.config.app_bin} {command}", pty=interactive, warn=True)
            else:
                self.stdout.output(
                    conn.run(f"{self.config.app_bin} {command}", hide=True).stdout
                )

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
            names = self._resolve_active_systemd_units(name)
            if not names:
                self.stdout.output("[yellow]No services found[/yellow]")
                return

            self.stdout.output(
                f"Running [cyan]{command}[/cyan] on: [cyan]{', '.join(names)}[/cyan]"
            )
            conn.run(f"sudo systemctl {command} {' '.join(names)}", pty=True)

        msg = f"{name} service" if name else "All Services"
        past_tense = {
            "start": "started",
            "restart": "restarted",
            "stop": "stopped",
        }.get(command, command)
        self.stdout.output(f"[green]{msg} {past_tense} successfully![/green]")

    @cappa.command(help="Show logs for the specified service")
    def logs(
        self,
        name: Annotated[str | None, cappa.Arg(help="Service name")] = None,
        follow: Annotated[bool, cappa.Arg(short="-f")] = False,
        lines: Annotated[int, cappa.Arg(short="-n", long="--lines")] = 50,
    ):
        with self.connection() as conn:
            names = self._resolve_active_systemd_units(name)
            if names:
                units = " ".join(f"-u {n}" for n in names)
                conn.run(
                    f"sudo journalctl {units} -n {lines} {'-f' if follow else ''}",
                    warn=True,
                    pty=True,
                )
            else:
                self.stdout.output("[yellow]No services found[/yellow]")

    def _resolve_active_systemd_units(self, name: str | None) -> list[str]:
        if not name:
            return self.config.active_systemd_units

        if name in self.config.processes:
            return self.config.get_active_unit_names(name)

        if name == "socket":
            has_socket = any(config.socket for config in self.config.processes.values())
            if has_socket:
                return [f"{self.config.app_name}.socket"]

        if name == "timer":
            return [n for n in self.config.active_systemd_units if n.endswith(".timer")]

        return [name]
