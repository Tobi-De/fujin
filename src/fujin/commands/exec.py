from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated

import cappa

from fujin.commands import BaseCommand


@cappa.command(
    help="Execute an arbitrary command on the server or via the app binary",
)
@dataclass
class Exec(BaseCommand):
    """
    Examples:
      fujin exec ls -la              Run command on server
      fujin exec --appenv ls         Run in app directory with app environment
      fujin exec --app shell         Run command via app binary (e.g., myapp shell)
    """

    command: Annotated[str, cappa.Arg(help="Command to execute")] = field(kw_only=True)
    appenv: Annotated[
        bool,
        cappa.Arg(
            long="--appenv",
            help="Change to app directory and enable app environment",
        ),
    ] = field(default=False, kw_only=True)
    app: Annotated[
        bool,
        cappa.Arg(
            long="--app",
            help="Execute command via the application binary",
        ),
    ] = field(default=False, kw_only=True)

    def __call__(self):
        if self.appenv and self.app:
            raise cappa.Exit(
                "Cannot use both --appenv and --app flags together", code=1
            )

        with self.connection() as conn:
            if self.app:
                # Run via app binary
                with conn.cd(self.config.app_dir(self.selected_host)):
                    conn.run(
                        f"source .appenv && {self.config.app_bin} {self.command}",
                        pty=True,
                    )
            elif self.appenv:
                # Run in app directory with app environment
                command = f"cd {self.config.app_dir(self.selected_host)} && source .appenv && {self.command}"
                conn.run(command, pty=True)
            else:
                # Plain server command
                conn.run(self.command, pty=True)
