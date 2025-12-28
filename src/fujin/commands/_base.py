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
        """Get formatted, colored list of available process and unit options."""
        options = []

        # Special values
        options.extend(["env", "caddy", "units"])

        # Unit type keywords
        if any(p.timer for p in self.config.processes.values()):
            options.append("timer")
        if any(p.socket for p in self.config.processes.values()):
            options.append("socket")

        # Process names
        options.extend(self.config.processes.keys())

        # Process name variations with suffixes
        for process_name, process_config in self.config.processes.items():
            options.append(f"{process_name}.service")
            if process_config.socket:
                options.append(f"{process_name}.socket")
            if process_config.timer:
                options.append(f"{process_name}.timer")

        # Apply uniform color to all options
        colored_options = [f"[cyan]{opt}[/cyan]" for opt in options]
        return " ".join(colored_options)

    def _resolve_units(
        self, name: str | None, use_templates: bool = False
    ) -> list[str]:
        """
        Resolve a process name to systemd unit names.

        Accepts process names (e.g., "web") and process names with suffixes
        (e.g., "web.service", "health.timer"). Does NOT accept full systemd
        names like "bookstore.service" or instance names like "bookstore-worker@1.service".

        Args:
            name: Process name or process name with suffix (.service/.timer/.socket)
                  Special keywords: "timer", "socket"
            use_templates: If True, return template names (for show/cat)
                          If False, return instance names (for start/stop/restart/logs)

        Returns:
            List of systemd unit names
        """

        if not name:
            return self.config.systemd_units

        # Extract base process name and suffix type
        suffix_type = None
        if name.endswith(".service"):
            process_name = name[:-8]
            suffix_type = "service"
        elif name.endswith(".timer"):
            process_name = name[:-6]
            suffix_type = "timer"
        elif name.endswith(".socket"):
            process_name = name[:-7]
            suffix_type = "socket"
        else:
            process_name = name

        # Handle special keywords
        if process_name == "timer":
            return [n for n in self.config.systemd_units if n.endswith(".timer")]

        if process_name == "socket":
            has_socket = any(config.socket for config in self.config.processes.values())
            if has_socket:
                return [f"{self.config.app_name}.socket"]
            return []

        # Validate process exists
        if process_name not in self.config.processes:
            available = ", ".join(self.config.processes.keys())
            raise cappa.Exit(
                f"Unknown process '{process_name}'. Available processes: {available}",
                code=1,
            )

        process_config = self.config.processes[process_name]
        units = []

        # If specific suffix requested, only return that unit type
        if suffix_type == "service":
            # Just the service unit(s)
            if use_templates:
                units.append(self.config.get_unit_template_name(process_name))
            else:
                units.extend(self.config.get_unit_names(process_name))

        elif suffix_type == "socket":
            # Just the socket unit
            if not process_config.socket:
                raise cappa.Exit(
                    f"Process '{process_name}' does not have a socket enabled.", code=1
                )
            units.append(f"{self.config.app_name}.socket")

        elif suffix_type == "timer":
            # Just the timer unit
            if not process_config.timer:
                raise cappa.Exit(
                    f"Process '{process_name}' does not have a timer enabled.", code=1
                )
            service_name = self.config.get_unit_template_name(process_name)
            timer_name = f"{service_name.replace('.service', '')}.timer"
            units.append(timer_name)

        else:
            # No suffix - return service + socket/timer if they exist
            if use_templates:
                units.append(self.config.get_unit_template_name(process_name))
            else:
                units.extend(self.config.get_unit_names(process_name))

            if process_config.socket:
                units.append(f"{self.config.app_name}.socket")

            if process_config.timer:
                service_name = self.config.get_unit_template_name(process_name)
                timer_name = f"{service_name.replace('.service', '')}.timer"
                units.append(timer_name)

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
