from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Annotated

import cappa
from rich.prompt import Confirm

from fujin import caddy
from fujin import serverops
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
    force: Annotated[
        bool,
        cappa.Arg(
            long="--force",
            help="Continue teardown even if a teardown step fails",
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

            errors = False

            # Stop and disable declared units.
            units = [u for u in self.config.systemd_units if u]
            if units:
                _, stopped = conn.run(
                    f"sudo systemctl disable --now {serverops.q(*units)}",
                    warn=True,
                    pty=True,
                )
                errors |= not stopped

            # Release-tree teardown: remove this app's unit files (incl.
            # stray instances), their dropin dirs and wants-symlinks,
            # then the app dir.
            app_q = shlex.quote(self.config.app_name)
            _, removed = conn.run(
                f"sudo rm -rf /etc/systemd/system/{app_q}*"
                f" && sudo rm -f /etc/systemd/system/*.wants/{app_q}*"
                f" && sudo rm -rf {app_dir_q}"
                f" && sudo systemctl daemon-reload",
                warn=True,
                pty=True,
            )
            errors |= not removed

            # Remove this app's Caddy config and reload the proxy
            # (reload failure is non-fatal: caddy may not be installed).
            if self.config.caddyfile_exists:
                __, caddy_ok = conn.run(
                    f"sudo rm -f {shlex.quote(self.config.caddy_config_path)}"
                    f" && sudo systemctl reload caddy",
                    warn=True,
                    pty=True,
                )
                if not caddy_ok:
                    self.output.warning(
                        "Could not remove the Caddy config and/or reload"
                        " caddy; the site config may need manual cleanup."
                    )

            # Terminate any leftover app-user processes and delete the
            # app user (both non-fatal when nothing/left to remove).
            app_user = self.config.app_user or self.config.app_name
            user_q = shlex.quote(app_user)
            _, userdel_ok = conn.run(
                f"sudo pkill -u {user_q} || true; sleep 1;"
                f" sudo pkill -9 -u {user_q} || true;"
                f" sudo userdel {user_q} || true",
                warn=True,
                pty=True,
            )
            errors |= not userdel_ok

            if errors:
                if not self.force:
                    raise cappa.Exit("Teardown failed", code=1)

                self.output.warning(
                    "Teardown encountered errors but continuing due to --force"
                )
                conn.run(f"sudo rm -rf {app_dir_q}", warn=True, pty=True)

            if self.full:
                conn.run(
                    "&& ".join(caddy.get_uninstall_commands()), pty=True, warn=True
                )

            log_operation(
                connection=conn,
                app_name=self.config.app_name,
                operation="full-down" if self.full else "down",
                host=self.selected_host.name or self.selected_host.address,
                version=self.config.version,
            )

        self.output.success("Project teardown completed successfully!")
