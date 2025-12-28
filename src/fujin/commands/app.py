from __future__ import annotations

import shlex
import subprocess
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
            app_dir = shlex.quote(self.config.app_dir)
            names = self.config.active_systemd_units
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
            suffix = ".tar.gz"
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

            services_status = {}
            statuses = parts[2].strip().split("\n")
            services_status = dict(zip(names, statuses))

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
    ):
        with self.connection() as conn:
            with conn.cd(self.config.app_dir):
                conn.run(f"source .appenv && {self.config.app_bin} {command}", pty=True)

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
        host = self.config.host
        ssh_target = f"{host.user}@{host.ip or host.domain_name}"
        ssh_cmd = ["ssh", "-t"]
        if host.ssh_port:
            ssh_cmd.extend(["-p", str(host.ssh_port)])
        if host.key_filename:
            ssh_cmd.extend(["-i", str(host.key_filename)])

        full_remote_cmd = f"cd {self.config.app_dir} && source .appenv && {command}"
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
                self.stdout.output(f"Showing logs for: [cyan]{', '.join(names)}[/cyan]")
                conn.run(
                    f"sudo journalctl {units} -n {lines} {'-f' if follow else ''}",
                    warn=True,
                    pty=True,
                )
            else:
                self.stdout.output("[yellow]No services found[/yellow]")

    @cappa.command(help="Show the systemd unit file content for the specified service")
    def cat(
        self,
        name: Annotated[str, cappa.Arg(help="Service name")],
    ):
        with self.connection() as conn:
            if name == "caddy" and self.config.webserver.enabled:
                # Special case for Caddy
                self.stdout.output(
                    f"Showing Caddy configuration at: [cyan]{self.config.caddy_config_path}[/cyan]"
                )
                print()
                conn.run(f"cat {self.config.caddy_config_path}")
                return

            names = self._resolve_active_systemd_units(name)

            if not names:
                self.stdout.output("[yellow]No services found[/yellow]")
                return

            conn.run(f"sudo systemctl cat {' '.join(names)}", pty=True)

    def _resolve_active_systemd_units(self, name: str | None) -> list[str]:
        """
        Resolve a user-provided name to a list of systemd unit names.

        This method handles various ways to specify services:
        - ``None``: Returns all active units defined in the configuration.
        - Process name (e.g., "web"): Returns the service unit(s) for that process,
          plus any associated socket or timer units.
        - "socket": Returns the main application socket if enabled.
        - "timer": Returns all timer units.
        - Specific unit (e.g., "web.service", "web.socket", "worker.timer"):
          Returns only that specific unit.

        If the name cannot be resolved, it raises a ``cappa.Exit`` with a list of
        available valid names.
        """
        if not name:
            return self.config.active_systemd_units

        if name in self.config.processes:
            units = self.config.get_active_unit_names(name)
            process_config = self.config.processes[name]
            if process_config.socket:
                units.append(f"{self.config.app_name}.socket")
            if process_config.timer:
                service_name = self.config.get_unit_template_name(name)
                timer_name = f"{service_name.replace('.service', '')}.timer"
                units.append(timer_name)
            return units

        if name == "socket":
            has_socket = any(config.socket for config in self.config.processes.values())
            if has_socket:
                return [f"{self.config.app_name}.socket"]

        if name == "timer":
            return [n for n in self.config.active_systemd_units if n.endswith(".timer")]

        if name.endswith(".socket"):
            process_name = name[:-7]
            if process_name in self.config.processes:
                if self.config.processes[process_name].socket:
                    return [f"{self.config.app_name}.socket"]
                raise cappa.Exit(
                    f"Process '{process_name}' does not have a socket enabled.", code=1
                )

        if name.endswith(".timer"):
            process_name = name[:-6]
            if process_name in self.config.processes:
                if self.config.processes[process_name].timer:
                    service_name = self.config.get_unit_template_name(process_name)
                    timer_name = f"{service_name.replace('.service', '')}.timer"
                    return [timer_name]
                raise cappa.Exit(
                    f"Process '{process_name}' does not have a timer enabled.", code=1
                )

        if name.endswith(".service"):
            process_name = name[:-8]
            if process_name in self.config.processes:
                return self.config.get_active_unit_names(process_name)

        options = []
        if any(p.timer for p in self.config.processes.values()):
            options.append("timer")
        if any(p.socket for p in self.config.processes.values()):
            options.append("socket")

        for process_name, process_config in self.config.processes.items():
            options.append(process_name)
            options.append(f"{process_name}.service")
            if process_config.socket:
                options.append(f"{process_name}.socket")
            if process_config.timer:
                options.append(f"{process_name}.timer")

        raise cappa.Exit(
            f"Unknown service '{name}'. Available services: {', '.join(options)}",
            code=1,
        )
