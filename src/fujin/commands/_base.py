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
    def deployed_units(self) -> list:
        """Cached deployed units for this command instance."""
        return self.config.deployed_units

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
