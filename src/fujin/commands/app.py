from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from typing import Annotated

import cappa
from rich.table import Table

from fujin.commands import BaseCommand


@cappa.command(
    help="Manage your application",
)
class App(BaseCommand):
    @cappa.command(help="Display application information and process status")
    def status(
        self,
        service: Annotated[
            str | None,
            cappa.Arg(
                help="Optional service name to show detailed info for a specific service"
            ),
        ] = None,
    ):
        # Check if we have any deployed units
        if not self.config.deployed_units:
            self.output.warning(
                "No services found in .fujin/systemd/\n"
                "Run 'fujin init' or 'fujin new service' to create services."
            )
            return

        # If service specified, show detailed info for that service only
        if service:
            return self._show_service_detail(service)

        names = []
        for du in self.config.deployed_units:
            names.extend(du.instance_service_names)
            if du.template_socket_name:
                names.append(du.template_socket_name)
            if du.template_timer_name:
                names.append(du.template_timer_name)

        with self.connection() as conn:
            app_dir = shlex.quote(self.config.app_dir)
            remote_version, _ = conn.run(
                f"cat {app_dir}/.version 2>/dev/null || echo N/A", warn=True, hide=True
            )
            remote_version = remote_version.strip()

            statuses_output, _ = conn.run(
                f"sudo systemctl is-active {' '.join(names)} 2>/dev/null || true",
                warn=True,
                hide=True,
            )
            statuses = (
                statuses_output.strip().split("\n") if statuses_output.strip() else []
            )
            services_status = dict(zip(names, statuses))

            infos = {
                "app_name": self.config.app_name,
                "local_version": self.config.version,
                "remote_version": remote_version,
            }
            if self.config.caddyfile_exists:
                domain = self.config.get_domain_name()
                if domain:
                    infos["running_at"] = f"https://{domain}"

            services = self._build_service_status_dict(services_status)

        # Display info and status table
        info_lines = [f"{key}: {value}" for key, value in infos.items()]
        self.output.output("\n".join(info_lines))
        self.output.output(self._build_status_table(services))

    def _build_service_status_dict(
        self, services_status: dict[str, str]
    ) -> dict[str, str]:
        """Build a dict of service name -> status string."""
        services = {}
        for du in self.config.deployed_units:
            running_count = sum(
                1
                for name in du.instance_service_names
                if services_status.get(name) == "active"
            )
            total_count = len(du.instance_service_names)

            if total_count == 1:
                services[du.service_name] = services_status.get(
                    du.instance_service_names[0], "unknown"
                )
            else:
                services[du.service_name] = f"{running_count}/{total_count}"

            if du.template_socket_name:
                socket_status = services_status.get(du.template_socket_name)
                if socket_status:
                    services[f"{du.service_name}.socket"] = socket_status

        return services

    def _build_status_table(self, services: dict[str, str]) -> Table:
        """Build a Rich table from service status dict."""
        table = Table(title="", header_style="bold cyan")
        table.add_column("Process", style="")
        table.add_column("Status")

        for service, status in services.items():
            status_str = self._format_status(status)
            table.add_row(service, status_str)

        return table

    def _format_status(self, status: str) -> str:
        """Format a status string with color."""
        styles = {
            "active": "bold green",
            "failed": "bold red",
            "inactive": "dim",
            "unknown": "dim",
        }
        if status in styles:
            return f"[{styles[status]}]{status}[/{styles[status]}]"
        if "/" in status:
            running, total = map(int, status.split("/"))
            style = (
                "bold green"
                if running == total
                else "bold red"
                if running == 0
                else "bold yellow"
            )
            return f"[{style}]{status}[/{style}]"
        return status

    def _show_service_detail(self, service_name: str):
        """Show detailed information for a specific service."""
        # Find the deployed unit
        deployed_unit = None
        for du in self.config.deployed_units:
            if du.service_name == service_name:
                deployed_unit = du
                break

        if not deployed_unit:
            self.output.error(
                f"Service '{service_name}' not found.\n"
                f"Available services: {', '.join(du.service_name for du in self.config.deployed_units)}"
            )
            return

        # Display service info
        source_file = deployed_unit.service_file.name
        deployed_file = deployed_unit.template_service_name

        self.output.output(f"[bold]Service:[/bold] {deployed_unit.service_name}")
        self.output.output(f"[bold]Source:[/bold] {source_file}")
        self.output.output(f"[bold]Deployed as:[/bold] {deployed_file}")
        if deployed_unit.is_template:
            self.output.output(f"[bold]Replicas:[/bold] {deployed_unit.replica_count}")

        # Get status from server
        with self.connection() as conn:
            # Get detailed status for each instance
            self.output.output("\n[bold]Status:[/bold]")
            for unit_name in deployed_unit.instance_service_names:
                # Get status with uptime info
                status_cmd = f"sudo systemctl show {unit_name} --property=ActiveState,SubState,LoadState,ActiveEnterTimestamp --no-pager"
                status_output, success = conn.run(status_cmd, warn=True, hide=True)

                if success:
                    # Parse systemctl show output
                    props = {}
                    for line in status_output.strip().split("\n"):
                        if "=" in line:
                            key, value = line.split("=", 1)
                            props[key] = value

                    active_state = props.get("ActiveState", "unknown")
                    load_state = props.get("LoadState", "unknown")
                    active_since = props.get("ActiveEnterTimestamp", "")

                    status_str = self._format_status(active_state)

                    if load_state == "not-found":
                        self.output.output(f"  {unit_name}: [dim]not deployed[/dim]")
                    else:
                        time_info = (
                            f" (since {active_since})"
                            if active_since and active_state == "active"
                            else ""
                        )
                        self.output.output(f"  {unit_name}: {status_str}{time_info}")
                else:
                    self.output.output(f"  {unit_name}: [dim]unknown[/dim]")

        # Show drop-ins if any
        drop_ins = self._find_dropins(deployed_unit)
        if drop_ins:
            self.output.output("\n[bold]Drop-ins:[/bold]")
            for dropin in drop_ins:
                self.output.output(f"  - {dropin}")

        # Show associated socket/timer if exists
        if deployed_unit.socket_file:
            self.output.output(
                f"\n[bold]Socket:[/bold] {deployed_unit.socket_file.name}"
            )
        if deployed_unit.timer_file:
            self.output.output(f"\n[bold]Timer:[/bold] {deployed_unit.timer_file.name}")

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
        ssh_target = f"{host.user}@{host.address}"
        ssh_cmd = ["ssh", "-t"]
        if host.port != 22:
            ssh_cmd.extend(["-p", str(host.port)])
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
            # Use instances for start/stop/restart (operates on running services)
            names = self._resolve_units(name, use_templates=False)
            if not names:
                self.output.warning("No services found")
                return

            # When stopping, also stop associated sockets
            if command == "stop" and name:
                du = next(
                    (u for u in self.deployed_units if u.service_name == name),
                    None,
                )
                if du and du.template_socket_name:
                    names.append(du.template_socket_name)

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
        Show last 50 lines for web process (default)
        """
        with self.connection() as conn:
            # Use instances for logs (shows logs from running services)
            names = self._resolve_units(name, use_templates=False)

            if names:
                units = " ".join(f"-u {n}" for n in names)

                cmd_parts = ["sudo journalctl", units]
                if not follow:
                    cmd_parts.append(f"-n {lines}")
                    cmd_parts.append("--no-pager")  # Prevent pager when not following
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

    @cappa.command(help="Show systemd unit file content, env file, or Caddyfile")
    def cat(
        self,
        name: Annotated[
            str | None,
            cappa.Arg(help="Service name, 'env', 'caddy', or 'units'"),
        ] = None,
    ):
        if not name:
            self.output.info("Available options:")
            self.output.output(self._get_available_options())
            return

        with self.connection() as conn:
            if name == "caddy" and self.config.caddyfile_exists:
                self.output.output(f"[cyan]# {self.config.caddy_config_path}[/cyan]")
                print()
                conn.run(f"cat {self.config.caddy_config_path}")
                print()
                return

            if name == "env":
                app_dir = shlex.quote(self.config.app_dir)
                env_path = f"{app_dir}/.env"
                self.output.output(f"[cyan]# {env_path}[/cyan]")
                print()
                conn.run(f"cat {env_path}", warn=True)
                print()
                return

            if name == "units":
                names = self.config.systemd_units
            else:
                names = self._resolve_units(name, use_templates=True)

            if not names:
                self.output.warning("No services found")
                return

            conn.run(f"sudo systemctl cat {' '.join(names)} --no-pager", pty=True)

    def _get_available_options(self) -> str:
        """Get formatted, colored list of available service and unit options."""
        options = []

        # Special values
        options.extend(["caddy", "env", "units"])

        # Service names and variations
        for du in self.deployed_units:
            options.append(du.service_name)
            if du.socket_file:
                options.append(f"{du.service_name}.socket")
            if du.timer_file:
                options.append(f"{du.service_name}.timer")

        # Apply uniform color to all options
        colored_options = [f"[cyan]{opt}[/cyan]" for opt in options]
        return " ".join(colored_options)

    def _resolve_units(
        self, name: str | None, use_templates: bool = False
    ) -> list[str]:
        """
        Resolve a service name to systemd unit names.

        Args:
            name: Service name (e.g., "web", "worker")
                  Can include suffix: "web.service", "worker.timer", "web.socket"
                  If None, returns all units
            use_templates: If True, return template names (for cat/show)
                          If False, return instance names (for start/stop/restart/logs)

        Returns:
            List of systemd unit names
        """
        if not name:
            return self.config.systemd_units

        suffixes = {".service": "service", ".timer": "timer", ".socket": "socket"}
        suffix_type = None
        service_name = name
        for suffix, stype in suffixes.items():
            if name.endswith(suffix):
                service_name = name.removesuffix(suffix)
                suffix_type = stype
                break

        du = next(
            (u for u in self.deployed_units if u.service_name == service_name),
            None,
        )
        if not du:
            available = ", ".join(u.service_name for u in self.deployed_units)
            raise cappa.Exit(
                f"Unknown service '{service_name}'. Available: {available}",
                code=1,
            )

        if suffix_type == "socket":
            if not du.socket_file:
                raise cappa.Exit(
                    f"Service '{service_name}' does not have a socket.", code=1
                )
            return [du.template_socket_name]

        if suffix_type == "timer":
            if not du.timer_file:
                raise cappa.Exit(
                    f"Service '{service_name}' does not have a timer.", code=1
                )
            return [du.template_timer_name]

        if use_templates:
            return [du.template_service_name]
        return du.instance_service_names

    def _find_dropins(self, deployed_unit) -> list[str]:
        """Find all dropin files for a deployed unit."""
        drop_ins = []

        # Common drop-ins
        common_dir = Path(".fujin/systemd/common.d")
        if common_dir.exists():
            common_files = list(common_dir.glob("*.conf"))
            drop_ins.extend([f"common.d/{f.name}" for f in common_files])

        # Service-specific drop-ins
        service_dropin_dir = Path(f".fujin/systemd/{deployed_unit.service_file.name}.d")
        if service_dropin_dir.exists():
            service_files = list(service_dropin_dir.glob("*.conf"))
            drop_ins.extend(
                [f"{deployed_unit.service_file.name}.d/{f.name}" for f in service_files]
            )

        return drop_ins
