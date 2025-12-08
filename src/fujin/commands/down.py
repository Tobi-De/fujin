from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Annotated

import cappa
from rich.prompt import Confirm

from fujin import caddy
from fujin.commands import BaseCommand, uninstall_archive_script


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
            f"and remove all configurations on the host {self.config.host.ip} for the project {self.config.app_name}.\n"
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
            self.stdout.output("[blue]Tearing down project...[/blue]")

            # Try remote script first
            app_dir = shlex.quote(self.config.app_dir)
            res, ok = conn.run(f"cat {app_dir}/.version", warn=True, hide=True)
            version = res.strip() if ok else self.config.version
            bundle_path = f"{app_dir}/.versions/{self.config.app_name}-{version}.tar.gz"

            uninstall_ok = False
            _, bundle_exists = conn.run(f"test -f {bundle_path}", warn=True, hide=True)

            if bundle_exists:
                uninstall_cmd = (
                    uninstall_archive_script(bundle_path, self.config.app_name, version)
                    + f"rm -rf {app_dir}"
                )
                _, uninstall_ok = conn.run(uninstall_cmd, warn=True, pty=True)

            if not uninstall_ok:
                if not self.force:
                    self.stdout.output("[red]Teardown failed[/red]")
                    self.stdout.output(
                        "[yellow]Use --force to ignore errors and continue teardown.[/yellow]"
                    )
                    raise cappa.Exit(code=1)

                self.stdout.output(
                    "[yellow]Teardown encountered errors but continuing due to --force[/yellow]"
                )

                # Local fallback
                new_units, user_units = self.config.render_systemd_units()
                context = self.config.build_context(
                    distfile_name=f"{self.config.app_name}-{version}.tar.gz",
                    user_units=user_units,
                    new_units=new_units,
                )
                uninstall_script = self.config.render_uninstall_script(context=context)
                combined = uninstall_script + f"\nrm -rf {app_dir}"
                conn.run(
                    f"bash -s <<'FUJIN_UNINSTALL'\n{combined}\nFUJIN_UNINSTALL",
                    pty=True,
                )

            if self.full:
                conn.run(
                    "&& ".join(caddy.get_uninstall_commands()), pty=True, warn=True
                )

            self.stdout.output(
                "[green]Project teardown completed successfully![/green]"
            )
