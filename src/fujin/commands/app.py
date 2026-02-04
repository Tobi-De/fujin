from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import cappa
import tomli_w
from rich.table import Table

from fujin.commands import BaseCommand
from fujin.config import tomllib
from fujin.discovery import DeployedUnit


@cappa.command(
    help="Manage your application",
)
@dataclass
class App(BaseCommand):
    @cappa.command(help="Display application information and process status")
    def status(
        self,
        services: Annotated[
            list[str],
            cappa.Arg(
                help="Optional service name(s) to show detailed info for specific services"
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

        # If services specified, show detailed info for those services only
        if services:
            for service in services:
                self._show_service_detail(service)
            return

        names = []
        for du in self.config.deployed_units:
            names.extend(du.service_instances())
            socket_name = du.template_socket_name
            timer_name = du.template_timer_name
            if socket_name:
                names.append(socket_name)
            if timer_name:
                names.append(timer_name)

        with self.connection() as conn:
            shlex.quote(self.config.app_dir)
            fujin_dir = shlex.quote(self.config.install_dir)
            remote_version, _ = conn.run(
                f"cat {fujin_dir}/.version 2>/dev/null || echo N/A",
                warn=True,
                hide=True,
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
                "local_version": self.config.local_version,
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
            instances = du.service_instances()
            running_count = sum(
                1 for name in instances if services_status.get(name) == "active"
            )
            total_count = len(instances)

            if total_count == 1:
                services[du.name] = services_status.get(instances[0], "unknown")
            else:
                services[du.name] = f"{running_count}/{total_count}"

            socket_name = du.template_socket_name
            if socket_name:
                socket_status = services_status.get(socket_name)
                if socket_status:
                    services[f"{du.name}.socket"] = socket_status

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
            if du.name == service_name:
                deployed_unit = du
                break

        if not deployed_unit:
            self.output.error(
                f"Service '{service_name}' not found.\n"
                f"Available services: {', '.join(du.name for du in self.config.deployed_units)}"
            )
            return

        # Display service info
        source_file = deployed_unit.service_file.name
        deployed_file = deployed_unit.template_service_name

        self.output.output(f"[bold]Service:[/bold] {deployed_unit.name}")
        self.output.output(f"[bold]Source:[/bold] {source_file}")
        self.output.output(f"[bold]Deployed as:[/bold] {deployed_file}")
        if deployed_unit.is_template:
            self.output.output(f"[bold]Replicas:[/bold] {deployed_unit.replicas}")

        # Get status from server
        with self.connection() as conn:
            # Get detailed status for each instance
            self.output.output("\n[bold]Status:[/bold]")
            for unit_name in deployed_unit.service_instances():
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

        full_remote_cmd = (
            f"cd {self.config.app_dir} && source .install/.appenv && {command}"
        )
        ssh_cmd.extend([ssh_target, full_remote_cmd])
        subprocess.run(ssh_cmd)

    @cappa.command(help="Execute command via the application binary")
    def exec(
        self,
        command: Annotated[str, cappa.Arg(help="Command to execute")],
    ):
        with self.connection() as conn:
            cmd = f"cd {self.config.app_dir} && source .install/.appenv && {self.config.app_bin} {command}"
            conn.run(
                f"sudo -u {self.config.app_user} bash -c {shlex.quote(cmd)}",
                pty=True,
            )

    @cappa.command(
        help="Start the specified service(s) or all services if no name is provided"
    )
    def start(
        self,
        names: Annotated[
            list[str], cappa.Arg(help="Service name(s), no value means all")
        ] = None,
    ):
        self._run_service_command("start", names)

    @cappa.command(
        help="Reload or restart the specified service(s) or all services if no name is provided"
    )
    def restart(
        self,
        names: Annotated[
            list[str], cappa.Arg(help="Service name(s), no value means all")
        ] = None,
        force: Annotated[
            bool,
            cappa.Arg(
                short="-f",
                long="--force",
                help="Force a full restart instead of reload-or-restart",
            ),
        ] = False,
    ):
        command = "restart" if force else "reload-or-restart"
        self._run_service_command(command, names)

    @cappa.command(
        help="Stop the specified service(s) or all services if no name is provided"
    )
    def stop(
        self,
        names: Annotated[
            list[str], cappa.Arg(help="Service name(s), no value means all")
        ] = None,
    ):
        self._run_service_command("stop", names)

    def _run_service_command(self, command: str, names: list[str] | None):
        with self.connection() as conn:
            # Use instances for start/stop/restart (operates on running services)
            units = self._get_runtime_units(names)
            if not units:
                self.output.warning("No services found")
                return

            # When stopping, also stop associated sockets
            if command == "stop" and names:
                for name in names:
                    du = next(
                        (u for u in self.deployed_units if u.name == name),
                        None,
                    )
                    socket_name = du.template_socket_name if du else None
                    if socket_name:
                        units.append(socket_name)

            self.output.output(
                f"Running [cyan]{command}[/cyan] on: [cyan]{', '.join(units)}[/cyan]"
            )
            conn.run(f"sudo systemctl {command} {' '.join(units)}", pty=True)

        msg = f"{', '.join(names)} service(s)" if names else "All Services"
        past_tense = {
            "start": "started",
            "restart": "restarted",
            "stop": "stopped",
        }.get(command, command)
        self.output.success(f"{msg} {past_tense} successfully!")

    @cappa.command(help="Show logs for the specified service(s)")
    def logs(
        self,
        names: Annotated[list[str], cappa.Arg(help="Service name(s)")] = None,
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
            units = self._get_runtime_units(names)

            if units:
                unit_args = " ".join(f"-u {n}" for n in units)

                cmd_parts = ["sudo journalctl", unit_args]
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

                self.output.output(f"Showing logs for: [cyan]{', '.join(units)}[/cyan]")
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
                fujin_dir = shlex.quote(self.config.install_dir)
                env_path = f"{fujin_dir}/.env"
                self.output.output(f"[cyan]# {env_path}[/cyan]")
                print()
                conn.run(f"cat {env_path}", warn=True)
                print()
                return

            if name == "units":
                names = self.config.systemd_units
            else:
                names = self._get_template_units(name)

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
            options.append(du.name)
            if du.socket_file:
                options.append(f"{du.name}.socket")
            if du.timer_file:
                options.append(f"{du.name}.timer")

        # Apply uniform color to all options
        colored_options = [f"[cyan]{opt}[/cyan]" for opt in options]
        return " ".join(colored_options)

    def _find_unit(self, name: str) -> DeployedUnit:
        """Find a deployed unit by name, raising an error if not found."""
        # Strip suffix if present
        service_name = name
        for suffix in (".service", ".timer", ".socket"):
            if name.endswith(suffix):
                service_name = name.removesuffix(suffix)
                break

        du = next(
            (u for u in self.deployed_units if u.name == service_name),
            None,
        )
        if not du:
            available = ", ".join(u.name for u in self.deployed_units)
            raise cappa.Exit(
                f"Unknown service '{service_name}'. Available: {available}",
                code=1,
            )
        return du

    def _get_runtime_units(self, names: list[str] | None) -> list[str]:
        """Get runtime units for start/stop/restart/logs."""
        if not names:
            return self.config.systemd_units

        units = []
        for name in names:
            du = self._find_unit(name)
            units.extend(du.service_instances())
        return units

    def _get_template_units(self, name: str) -> list[str]:
        """Get template units for cat/show commands."""
        # Handle suffix-specific requests
        if name.endswith(".socket"):
            du = self._find_unit(name)
            socket_name = du.template_socket_name
            if not socket_name:
                raise cappa.Exit(f"Service '{du.name}' does not have a socket.", code=1)
            return [socket_name]

        if name.endswith(".timer"):
            du = self._find_unit(name)
            timer_name = du.template_timer_name
            if not timer_name:
                raise cappa.Exit(f"Service '{du.name}' does not have a timer.", code=1)
            return [timer_name]

        du = self._find_unit(name)
        return [du.template_service_name]

    def _find_dropins(self, deployed_unit: DeployedUnit) -> list[str]:
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

    @cappa.command(help="Scale a service to run multiple replicas")
    def scale(
        self,
        service: Annotated[
            str,
            cappa.Arg(help="Name of the service to scale (e.g., 'worker', 'web')"),
        ],
        count: Annotated[
            int,
            cappa.Arg(
                help="Number of replicas (1 for single instance, 2+ for template)"
            ),
        ],
    ):
        if count == 0:
            self.output.error(
                f"Cannot scale to 0. To stop the service, use:\n"
                f"  fujin app stop {service}\n\n"
                f"To remove the service entirely, delete the files manually:\n"
                f"  rm .fujin/systemd/{service}*.service .fujin/systemd/{service}*.socket"
            )
            raise cappa.Exit(code=1)

        if count < 0:
            self.output.error("Replica count must be 1 or greater")
            raise cappa.Exit(code=1)

        systemd_dir = Path(".fujin/systemd")
        deployed_unit = None
        for unit in self.config.deployed_units:
            if unit.name == service:
                deployed_unit = unit
                break

        if not deployed_unit:
            self.output.error(
                f"Service '{service}' not found in {systemd_dir}/\n"
                f"Use 'fujin new service {service}' to create it first."
            )
            raise cappa.Exit(code=1)

        old_replica_count = deployed_unit.replicas
        old_is_template = deployed_unit.is_template
        needs_conversion = False

        if count == 1:
            # Scale to 1 - convert template to regular or keep regular
            if deployed_unit.is_template:
                # Convert template to regular
                template_service = systemd_dir / f"{service}@.service"
                regular_service = systemd_dir / f"{service}.service"
                content = template_service.read_text()
                # Remove %i, %I template specifiers (basic conversion)
                content = content.replace("%i", "").replace("%I", "")
                regular_service.write_text(content)
                template_service.unlink()
                self.output.success(
                    f"Converted {template_service.name} → {regular_service.name}"
                )
                needs_conversion = True
            else:
                self.output.info(f"{service} already configured for single instance")
            self._update_replicas_config(service, None)

        else:
            # Scale to 2+ - convert to template or update
            if not deployed_unit.is_template:
                # Convert regular to template
                regular_service = systemd_dir / f"{service}.service"
                template_service = systemd_dir / f"{service}@.service"
                content = regular_service.read_text()
                # Add %i to Description if it contains the service name
                if f"{{{{app_name}}}} {service}" in content:
                    content = content.replace(
                        f"{{{{app_name}}}} {service}",
                        f"{{{{app_name}}}} {service} %i",
                    )
                template_service.write_text(content)
                regular_service.unlink()
                self.output.success(
                    f"Converted {regular_service.name} → {template_service.name}"
                )
                needs_conversion = True
            else:
                self.output.info(f"{service} already configured as template")

            if deployed_unit.socket_file:
                self.output.warning(
                    f"\n[bold]Warning: Scaling a socket-activated service is not recommended.[/bold]\n\n"
                    f"Socket file {deployed_unit.socket_file.name} found. Sockets don't scale well because:\n"
                    f"  - Only one socket exists for all replicas\n"
                    f"  - Socket activation happens per-connection, not per-replica\n"
                    f"  - Your web server likely has built-in concurrency/worker settings\n\n"
                    f"Instead of scaling replicas, configure your web server:\n"
                    f"  - Gunicorn: --workers N or --threads N\n"
                    f"  - Uvicorn: --workers N\n"
                    f"  - Other servers: check their concurrency/worker documentation\n"
                )
            self._update_replicas_config(service, count)

        # Only scale on server if:
        # 1. No conversion needed (service was already a template)
        # 2. Count changed
        if needs_conversion:
            self.output.info(
                f"\nNext steps:\n  1. Deploy to apply changes: fujin deploy"
            )
        elif old_replica_count != count and old_is_template:
            self._scale_on_server(service, old_replica_count, count)
        elif old_replica_count == count:
            self.output.info(f"\nService already at {count} replica(s)")

    def _scale_on_server(self, service: str, old_count: int, new_count: int):
        with self.connection() as conn:
            app_name = self.config.app_name

            if new_count > old_count:
                # Scaling up - start new instances
                self.output.info(
                    f"Scaling up {service} from {old_count} to {new_count} instances..."
                )
                new_instances = [
                    f"{app_name}-{service}@{i}.service"
                    for i in range(old_count + 1, new_count + 1)
                ]
                instances_str = " ".join(new_instances)
                _, success = conn.run(
                    f"sudo systemctl start {instances_str} && sudo systemctl enable {instances_str}"
                )
                if success:
                    self.output.success(f"Started {len(new_instances)} new instance(s)")
                else:
                    self.output.error(f"Failed to start new instances")

            elif new_count < old_count:
                # Scaling down - stop old instances
                self.output.info(
                    f"Scaling down {service} from {old_count} to {new_count} instances..."
                )
                removed_instances = [
                    f"{app_name}-{service}@{i}.service"
                    for i in range(new_count + 1, old_count + 1)
                ]
                instances_str = " ".join(removed_instances)
                _, success = conn.run(
                    f"sudo systemctl stop {instances_str} && sudo systemctl disable {instances_str}"
                )
                if success:
                    self.output.success(f"Stopped {len(removed_instances)} instance(s)")
                else:
                    self.output.error(f"Failed to stop instances")

    def _update_replicas_config(self, service_name: str, count: int | None):
        fujin_toml = Path("fujin.toml")
        if not fujin_toml.exists():
            return

        config_dict = tomllib.loads(fujin_toml.read_text())
        replicas = config_dict.get("replicas", {})

        if count is None:
            if service_name in replicas:
                del replicas[service_name]
                self.output.success(f"Removed replica config for {service_name}")
        else:
            replicas[service_name] = count
            self.output.success(
                f"Updated fujin.toml: {service_name} = {count} replicas"
            )

        config_dict["replicas"] = replicas
        if not replicas:
            del config_dict["replicas"]

        fujin_toml.write_text(tomli_w.dumps(config_dict, multiline_strings=True))
