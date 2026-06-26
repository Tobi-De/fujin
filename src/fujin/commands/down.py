from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Annotated

import cappa
from rich.prompt import Confirm

from fujin import caddy
from fujin.audit import log_operation
from fujin.commands import BaseCommand
from fujin import connection


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
        msg = (
            f"[red]You are about to delete all project files, stop all services,\n"
            f"remove the app user ({self.config.app_user}), and remove all configurations\n"
            f"on the host {self.selected_host.address} for the project {self.config.app_name}.\n"
            f"Any assets in your project folder will be lost.\n"
            f"Are you sure you want to proceed? This action is irreversible.[/red]"
        )
        try:
            confirm = Confirm.ask(msg)
        except KeyboardInterrupt:
            raise cappa.Exit("Teardown aborted", code=0)
        if not confirm:
            return

        with connection.connection(host=self.selected_host) as conn:
            self.output.info("Tearing down project...")

            app_dir_q = shlex.quote(self.config.app_dir)
            current_dir = shlex.quote(f"{self.config.app_dir}/current")
            res, ok = conn.run(f"cat {current_dir}/.version", warn=True, hide=True)
            version = res.strip() if ok else self.config.version

            # Stop and remove systemd units, Caddy config, then remove app directory
            conn.run(
                f"systemctl stop {self.config.app_name}-*.service 2>/dev/null; "
                f"systemctl disable {self.config.app_name}-* 2>/dev/null; "
                f"rm -f /etc/systemd/system/{self.config.app_name}-*; "
                f"systemctl daemon-reload; "
                f"rm -f /etc/caddy/conf.d/{self.config.app_name}.caddy; "
                f"systemctl reload caddy 2>/dev/null; "
                f"rm -rf {app_dir_q}",
                warn=True,
                pty=True,
            )

            # Remove app user
            conn.run(
                f"userdel -r {self.config.app_user} 2>/dev/null || true", warn=True
            )

            if self.full:
                conn.run(
                    "&& ".join(caddy.get_uninstall_commands()), pty=True, warn=True
                )

            log_operation(
                connection=conn,
                app_name=self.config.app_name,
                operation="full-down" if self.full else "down",
                host=self.selected_host.name or self.selected_host.address,
                version=version,
            )

        self.output.success("Project teardown completed successfully!")
