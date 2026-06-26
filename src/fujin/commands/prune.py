from dataclasses import dataclass
from typing import Annotated

import cappa
from rich.prompt import Confirm

from fujin.commands import BaseCommand
from fujin import connection


@cappa.command(
    help="Prune old artifacts, keeping only the specified number of recent versions"
)
@dataclass
class Prune(BaseCommand):
    keep: Annotated[
        int | None,
        cappa.Arg(
            short="-k",
            long="--keep",
            help="Number of version artifacts to retain (minimum 1). Defaults to versions_to_keep from config",
        ),
    ] = None

    def __call__(self):
        keep = (
            self.keep if self.keep is not None else (self.config.versions_to_keep or 5)
        )

        if keep < 1:
            raise cappa.Exit("The minimum value for the --keep option is 1", code=1)

        releases_dir = f"{self.config.app_dir}/releases"
        with connection.connection(host=self.selected_host) as conn:
            _, success = conn.run(f"test -d {releases_dir}", warn=True, hide=True)
            if not success:
                self.output.info("No releases directory found. Nothing to prune.")
                return

            result, _ = conn.run(f"ls -1t {releases_dir}", warn=True, hide=True)

            if not result:
                self.output.info("No releases found to prune")
                return

            all_releases = [d for d in result.strip().splitlines() if d]

            if len(all_releases) <= keep:
                self.output.info(
                    f"Only {len(all_releases)} release(s) found. Nothing to prune (keep={keep})."
                )
                return

            to_delete = all_releases[keep:]

            if not Confirm.ask(
                f"[red]The following releases will be permanently deleted: {', '.join(to_delete)}.\\n"
                f"This action is irreversible. Are you sure you want to proceed?[/red]"
            ):
                return

            for d in to_delete:
                conn.run(f"rm -rf {releases_dir}/{d}")

            self.output.success("Pruning completed successfully")
