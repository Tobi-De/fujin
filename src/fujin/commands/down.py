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

            command = "uninstall-full" if self.full else "uninstall"
            # Try remote script first
            app_dir = self.config.app_dir
            res, ok = conn.run(f"cat {app_dir}/.version", warn=True, hide=True)
            version = res.strip() if ok else self.config.version

            bundle_path = f"{app_dir}/.versions/{self.config.app_name}-{version}.tar.gz"
            remote_script = f"/tmp/setup-{self.config.app_name}-{version}"

            # Extract setup script
            cmd = f"tar -xzf {bundle_path} -O setup > {remote_script} && chmod +x {remote_script}"
            res, ok = conn.run(cmd, warn=True)
            if ok:
                _, result_ok = conn.run(
                    f"bash {remote_script} {command}", warn=True, pty=True
                )
                conn.run(f"rm -f {remote_script}", warn=True)
                if result_ok:
                    self.stdout.output(
                        "[green]Project teardown completed successfully![/green]"
                    )
                    return

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
            valid_units = set(self.config.active_systemd_units) | set(new_units.keys())
            valid_units_str = " ".join(sorted(valid_units))
            setup_script = self.config.render_setup_script(
                distfile_name="",  # Not needed for uninstall
                valid_units_str=valid_units_str,
                user_units=user_units,
            )

            # Upload script to a temporary location
            remote_script_path = (
                f"/tmp/setup-{self.config.app_name}-{self.config.version}-local"
            )
            conn.run(
                f"cat << 'EOF' > {remote_script_path}\n{setup_script}\nEOF && chmod +x {remote_script_path}"
            )
            conn.run(
                f"bash {remote_script_path} {command} && rm -f {remote_script_path}",
                pty=True,
            )
            self.stdout.output(
                "[green]Project teardown completed successfully![/green]"
            )
