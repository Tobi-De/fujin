from __future__ import annotations

import logging
import shlex
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import cached_property
from typing import Annotated, Protocol

import cappa

from fujin.config import Config, HostConfig
from fujin.redaction import install_log_filter, redact

_logging_configured = False


def app_env_command(app_dir: str, command: str) -> str:
    """Run ``command`` with the deployed ``current/.env`` exported.

    The generated env file uses shell assignment syntax (systemd loads it as an
    ``EnvironmentFile``). Sourcing it alone only creates shell variables, which
    a child process such as the app binary or an interactive shell never
    inherits; ``set -a`` turns the assignments into exported environment
    variables for the duration of the command.
    """
    return (
        f"cd {shlex.quote(app_dir)} && set -a && . current/.env && set +a && {command}"
    )


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

    verbose: Annotated[
        int,
        cappa.Arg(
            short="-v",
            long="--verbose",
            count=True,
            help="Increase verbosity (-v, -vv, -vvv)",
        ),
    ] = 0

    def __post_init__(self) -> None:
        global _logging_configured

        if not _logging_configured:
            if self.verbose == 0:
                level = logging.WARNING
            elif self.verbose == 1:
                level = logging.INFO
            else:
                level = logging.DEBUG
            logging.basicConfig(level=level, format="%(message)s")
            logging.getLogger("markdown_it").setLevel(logging.WARNING)

            _logging_configured = True

    @cached_property
    def config(self) -> Config:
        return Config.read(self.host)

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


class LiveStatus(Protocol):
    """What a :meth:`MessageFormatter.status` block exposes to callers."""

    def update(self, status: str) -> None: ...
    def stop(self) -> None: ...
    def start(self) -> None: ...


class MessageFormatter:
    """Enhanced output with built-in color formatting for consistent CLI messaging."""

    def __init__(self, output: cappa.Output):
        self._output = output
        # Every command's user-facing output goes through a
        # MessageFormatter; attaching the filter here covers logging on all
        # command paths as well (deploy, rollback, app logs, ...).
        install_log_filter()

    def success(self, message: str):
        """Print success message (green)."""
        self._output.output(f"[green]{redact(message)}[/green]")

    def error(self, message: str):
        """Print error message (red)."""
        self._output.output(f"[red]{redact(message)}[/red]")

    def warning(self, message: str):
        """Print warning message (yellow)."""
        self._output.output(f"[yellow]{redact(message)}[/yellow]")

    def info(self, message: str):
        """Print info/progress message (blue)."""
        self._output.output(f"[blue]{redact(message)}[/blue]")

    def output(self, message: str):
        """Print plain message (for custom formatting)."""
        self._output.output(redact(message))

    @contextmanager
    def status(self, message: str) -> Iterator[LiveStatus | None]:
        """Run a live spinner while a long local step runs.

        Yields the Rich ``Status`` (``.update(text)`` changes the display) or
        ``None`` when output is not a terminal: there the message is printed
        once as a normal info line so piped/CI logs still show the step.
        """
        console = self._output.output_console
        if not console.is_terminal:
            self.info(message)
            yield None
            return
        with console.status(f"[blue]{redact(message)}[/blue]", spinner="dots") as live:
            yield live
