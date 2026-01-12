from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from functools import cached_property
from typing import Annotated, Generator

import cappa

from fujin.config import Config, HostConfig
from fujin.connection import SSH2Connection
from fujin.connection import connection as host_connection


@dataclass
class BaseCommand:
    """
    A command that provides access to the host config and provide a connection to interact with it,
    including configuring the web proxy and managing systemd services.
    """

    host: Annotated[
        str | None,
        cappa.Arg(
            short="-H",
            long="--host",
            help="Target host (for multi-host setups). Defaults to first host.",
        ),
    ] = None

    @cached_property
    def config(self) -> Config:
        return Config.read()

    @cached_property
    def selected_host(self) -> HostConfig:
        """Get the selected host based on --host flag or default."""
        return self.config.select_host(self.host)

    @cached_property
    def output(self) -> MessageFormatter:
        return MessageFormatter(cappa.Output())

    @contextmanager
    def connection(self) -> Generator[SSH2Connection, None, None]:
        with host_connection(host=self.selected_host) as conn:
            yield conn

    def _get_available_options(self) -> str:
        """Get formatted, colored list of available service and unit options."""
        options = []

        # Special values
        options.extend(["env", "caddy", "units"])

        # Get discovered services from .fujin/systemd/
        discovered = self.config.discovered_services

        has_timer = any(svc.timer_file for svc in discovered)
        has_socket = any(svc.socket_file for svc in discovered)

        if has_timer:
            options.append("timer")
        if has_socket:
            options.append("socket")

        # Service names and variations
        for svc in discovered:
            options.append(svc.name)
            options.append(f"{svc.name}.service")
            if svc.socket_file:
                options.append(f"{svc.name}.socket")
            if svc.timer_file:
                options.append(f"{svc.name}.timer")

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

        # Get discovered services
        discovered = self.config.discovered_services
        svc = next((s for s in discovered if s.name == service_name), None)
        if not svc:
            available = ", ".join(s.name for s in discovered)
            raise cappa.Exit(
                f"Unknown service '{service_name}'. Available services: {available}",
                code=1,
            )

        units = []
        replicas = self.config.replicas.get(svc.name, 1)

        # Build deployed unit names
        if suffix_type == "service" or suffix_type is None:
            if use_templates and svc.is_template:
                units.append(f"{self.config.app_name}-{svc.name}@.service")
            else:
                if svc.is_template:
                    base = f"{self.config.app_name}-{svc.name}"
                    units.extend(
                        [f"{base}@{i}.service" for i in range(1, replicas + 1)]
                    )
                else:
                    units.append(f"{self.config.app_name}-{svc.name}.service")

        if suffix_type == "socket" or (suffix_type is None and svc.socket_file):
            if not svc.socket_file and suffix_type == "socket":
                raise cappa.Exit(
                    f"Service '{service_name}' does not have a socket.", code=1
                )
            if svc.socket_file:
                units.append(
                    f"{self.config.app_name}-{svc.name}{'@' if svc.is_template else ''}.socket"
                )

        if suffix_type == "timer" or (suffix_type is None and svc.timer_file):
            if not svc.timer_file and suffix_type == "timer":
                raise cappa.Exit(
                    f"Service '{service_name}' does not have a timer.", code=1
                )
            if svc.timer_file:
                units.append(
                    f"{self.config.app_name}-{svc.name}{'@' if svc.is_template else ''}.timer"
                )

        return units


class MessageFormatter:
    """Enhanced output with built-in color formatting for consistent CLI messaging."""

    def __init__(self, output: cappa.Output):
        self._output = output

    def success(self, message: str):
        """Print success message (green)."""
        self._output.output(f"[green]{message}[/green]")

    def error(self, message: str):
        """Print error message (red)."""
        self._output.output(f"[red]{message}[/red]")

    def warning(self, message: str):
        """Print warning message (yellow)."""
        self._output.output(f"[yellow]{message}[/yellow]")

    def info(self, message: str):
        """Print info/progress message (blue)."""
        self._output.output(f"[blue]{message}[/blue]")

    def critical(self, message: str):
        """Print critical message (bold red)."""
        self._output.output(f"[bold red]{message}[/bold red]")

    def output(self, message: str):
        """Print plain message (for custom formatting)."""
        self._output.output(message)

    def link(self, url: str, text: str | None = None) -> str:
        """Format clickable URL link (returns string for inline use)."""
        display = text or url
        return f"[link={url}]{display}[/link]"

    def dim(self, message: str) -> str:
        """Format dimmed/secondary text (returns string for inline use)."""
        return f"[dim]{message}[/dim]"
