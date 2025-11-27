from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

import cappa
from rich.prompt import Confirm

from fujin import caddy
from fujin.commands import BaseCommand


@cappa.command(
    help="Tear down the project by stopping services and cleaning up resources"
)
@dataclass
class Down(BaseCommand):
    full: Annotated[
        bool,
        cappa.Arg(
            short="-f",
            long="--full",
            help="Stop and uninstall proxy as part of teardown",
        ),
    ] = False

    def __call__(self):
        try:
            confirm = Confirm.ask(
                f"""[red]You are about to delete all project files, stop all services, 
                and remove all configurations on the host {self.config.host.ip} for the project {self.config.app_name}. 
                Any assets in your project folder will be lost (sqlite not in there ?). 
                Are you sure you want to proceed? This action is irreversible.[/red]""",
            )
        except KeyboardInterrupt:
            raise cappa.Exit("Teardown aborted", code=0)
        if not confirm:
            return
        with self.connection() as conn:
            conn.run(f"rm -rf {self.config.app_dir}")
            if self.config.webserver.enabled:
                caddy.teardown(conn, self.config)

            active_systemd_units = self.config.active_systemd_units
            self.stdout.output(
                f"[blue]Stopping and disabling services: {' '.join(active_systemd_units)}[/blue]"
            )
            conn.run(
                f"sudo systemctl disable --now {' '.join(active_systemd_units)}",
                warn=True,
            )
            # Remove service files
            unit_files = list(self.config.render_systemd_units().keys())
            paths = [f"/etc/systemd/system/{name}" for name in unit_files]
            conn.run(f"sudo rm {' '.join(paths)}", warn=True)

            conn.run("sudo systemctl daemon-reload")
            conn.run("sudo systemctl reset-failed")

            if self.full and self.config.webserver.enabled:
                caddy.uninstall(conn)
            self.stdout.output(
                "[green]Project teardown completed successfully![/green]"
            )
