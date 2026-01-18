from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Annotated

import cappa
from rich.prompt import Confirm

from fujin import caddy
from fujin.commands import BaseCommand
from fujin.audit import log_operation


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
            help="Continue teardown even if uninstall script fails",
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

        with self.connection() as conn:
            self.output.info("Tearing down project...")

            app_dir = shlex.quote(self.config.app_dir)
            fujin_dir = shlex.quote(self.config.install_dir)
            res, ok = conn.run(f"cat {fujin_dir}/.version", warn=True, hide=True)
            version = res.strip() if ok else self.config.version
            bundle_path = f"{fujin_dir}/.versions/{self.config.app_name}-{version}.pyz"

            _, bundle_exists = conn.run(f"test -f {bundle_path}", warn=True, hide=True)

            uninstall_ok = False
            if bundle_exists:
                uninstall_cmd = (
                    f"python3 {bundle_path} uninstall && sudo rm -rf {app_dir}"
                )
                _, uninstall_ok = conn.run(uninstall_cmd, warn=True, pty=True)

            if not uninstall_ok:
                if not self.force:
                    raise cappa.Exit("Teardown failed", code=1)

                self.output.warning(
                    "Teardown encountered errors but continuing due to --force"
                )
                conn.run(f"sudo rm -rf {app_dir}", warn=True, pty=True)

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
