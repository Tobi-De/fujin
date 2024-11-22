from dataclasses import dataclass

import cappa
from rich.prompt import Confirm
from rich.prompt import Prompt

from fujin.commands import BaseCommand
from fujin.commands.deploy import Deploy


@cappa.command(help="Rollback application to a previous version")
@dataclass
class Rollback(BaseCommand):
    def __call__(self):
        with self.app_environment() as conn:
            result = conn.run(
                "sed -n '2,$p' .versions", warn=True, hide=True
            ).stdout.strip()
            if not result:
                self.stdout.output("[blue]No rollback targets available")
                return
            versions: list[str] = result.split("\n")
            try:
                version = Prompt.ask(
                    "Enter the version you want to rollback to:",
                    choices=versions,
                    default=versions[0],
                )
            except KeyboardInterrupt as e:
                raise cappa.Exit("Rollback aborted by user.", code=0) from e

            current_app_version = conn.run(
                "head -n 1 .versions", warn=True, hide=True
            ).stdout.strip()
            versions_to_clean = [current_app_version] + versions[
                : versions.index(version)
            ]
            confirm = Confirm.ask(
                f"[blue]Rolling back to v{version} will permanently delete versions {', '.join(versions_to_clean)}. This action is irreversible. Are you sure you want to proceed?[/blue]"
            )
            if not confirm:
                return
            deploy = Deploy()
            deploy.install_project(conn, version)
            self.create_process_manager(conn).restart_services()
            conn.run(f"rm -r {' '.join(f'v{v}' for v in versions_to_clean)}", warn=True)
            conn.run(f"sed -i '1,/{version}/{{/{version}/!d}}' .versions", warn=True)
            self.stdout.output(
                f"[green]Rollback to version {version} from {current_app_version} completed successfully![/green]"
            )
