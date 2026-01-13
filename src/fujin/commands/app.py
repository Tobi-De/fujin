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
            app_dir = shlex.quote(self.config.app_dir(self.selected_host))
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

                    # Format status with color
                    if active_state == "active":
                        status_str = f"[bold green]{active_state}[/bold green]"
                    elif active_state == "failed":
                        status_str = f"[bold red]{active_state}[/bold red]"
                    else:
                        status_str = f"[dim]{active_state}[/dim]"

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
            # Get timer status
            with self.connection() as conn:
                timer_name = deployed_unit.template_timer_name
                timer_cmd = f"sudo systemctl show {timer_name} --property=NextElapseUSecRealtime,LastTriggerUSec --no-pager"
                timer_output, success = conn.run(timer_cmd, warn=True, hide=True)
                if success:
                    props = {}
                    for line in timer_output.strip().split("\n"):
                        if "=" in line:
                            key, value = line.split("=", 1)
                            props[key] = value
                    next_run = props.get("NextElapseUSecRealtime", "")
                    if next_run and next_run != "0":
                        self.output.output(f"  Next run: {next_run}")

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
        from pathlib import Path

        with self.connection() as conn:
            # Use instances for start/stop/restart (operates on running services)
            names = self._resolve_units(name, use_templates=False)
            if not names:
                self.output.warning("No services found")
                return

            # When stopping a service, also stop associated sockets
            # Check for socket files in .fujin/systemd/ directory
            if command == "stop" and name:
                systemd_dir = Path(".fujin/systemd")
                if systemd_dir.exists():
                    # Check for socket file matching the service name
                    socket_file = systemd_dir / f"{name}.socket"
                    if socket_file.exists():
                        socket_unit = f"{self.config.app_name}-{name}.socket"
                        if socket_unit not in names:
                            names.append(socket_unit)
                            self.output.info(f"Also stopping socket: {socket_unit}")

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
            if name == "caddy" and self.config.caddyfile_exists:
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

            conn.run(f"sudo systemctl cat {' '.join(names)} --no-pager", pty=True)

    def _get_available_options(self) -> str:
        """Get formatted, colored list of available service and unit options."""
        options = []

        # Special values
        options.extend(["env", "caddy", "units"])

        # Get deployed units
        has_timer = any(du.timer_file for du in self.deployed_units)
        has_socket = any(du.socket_file for du in self.deployed_units)

        if has_timer:
            options.append("timer")
        if has_socket:
            options.append("socket")

        # Service names and variations
        for du in self.deployed_units:
            options.append(du.service_name)
            options.append(f"{du.service_name}.service")
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

        Accepts service names (e.g., "web") and service names with suffixes
        (e.g., "web.service", "health.timer"). Does NOT accept full systemd
        names like "bookstore.service" or instance names like "bookstore-worker@1.service".

        Args:
            name: Service name or service name with suffix (.service/.timer/.socket)
                  Special keywords: "timer", "socket"
            use_templates: If True, return template names (for show/cat)
                          If False, return instance names (for start/stop/restart/logs)

        Returns:
            List of systemd unit names
        """

        systemd_units = self.config.systemd_units

        if not name:
            return systemd_units

        # Extract base service name and suffix type
        suffix_type = None
        if name.endswith(".service"):
            service_name = name[:-8]
            suffix_type = "service"
        elif name.endswith(".timer"):
            service_name = name[:-6]
            suffix_type = "timer"
        elif name.endswith(".socket"):
            service_name = name[:-7]
            suffix_type = "socket"
        else:
            service_name = name

        # Handle special keywords
        if service_name == "timer":
            return [n for n in systemd_units if n.endswith(".timer")]

        if service_name == "socket":
            return [n for n in systemd_units if n.endswith(".socket")]

        # Find the deployed unit
        du = next(
            (u for u in self.deployed_units if u.service_name == service_name),
            None,
        )
        if not du:
            available = ", ".join(u.service_name for u in self.deployed_units)
            raise cappa.Exit(
                f"Unknown service '{service_name}'. Available services: {available}",
                code=1,
            )

        units = []

        # Build unit names based on suffix type and use_templates flag
        if suffix_type == "service" or suffix_type is None:
            if use_templates:
                units.append(du.template_service_name)
            else:
                units.extend(du.instance_service_names)

        if suffix_type == "socket" or (suffix_type is None and du.socket_file):
            if not du.socket_file and suffix_type == "socket":
                raise cappa.Exit(
                    f"Service '{service_name}' does not have a socket.", code=1
                )
            if du.template_socket_name:
                units.append(du.template_socket_name)

        if suffix_type == "timer" or (suffix_type is None and du.timer_file):
            if not du.timer_file and suffix_type == "timer":
                raise cappa.Exit(
                    f"Service '{service_name}' does not have a timer.", code=1
                )
            if du.template_timer_name:
                units.append(du.template_timer_name)

        return units
