from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Annotated

import cappa
from rich.prompt import Confirm

from fujin import connection, serverops, store
from fujin.commands import BaseCommand


@cappa.command(
    help="Prune old releases, keeping only the specified number of recent ones"
)
@dataclass
class Prune(BaseCommand):
    keep: Annotated[
        int | None,
        cappa.Arg(
            short="-k",
            long="--keep",
            help="Number of releases to retain, including current and previous "
            "(minimum 2). Defaults to versions_to_keep from config.",
        ),
    ] = None

    def __call__(self):
        keep = (
            self.keep if self.keep is not None else (self.config.versions_to_keep or 5)
        )

        if keep < 2:
            raise cappa.Exit(
                "The minimum value for the --keep option is 2 (current + previous"
                " are always kept)",
                code=1,
            )

        app_dir = self.config.app_dir
        releases_dir = f"{app_dir}/releases"
        with connection.connection(host=self.selected_host) as conn:
            state = serverops.read_state(conn, app_dir)
            if state is None:
                self.output.info("No release store state found. Nothing to prune.")
                return

            releases = serverops.list_releases(conn, app_dir)
            if not releases:
                self.output.info("No releases found to prune.")
                return

            kill = store.gc_plan(
                state.current,
                state.previous,
                releases,
                keep,
            )
            if not kill:
                self.output.info(
                    f"Only {len(releases)} releases found. Nothing to prune (keep={keep})."
                )
                return

            if not Confirm.ask(
                f"[red]The following releases will be permanently deleted:"
                f" {', '.join(kill)}.\\n"
                f"This action is irreversible. Are you sure you want to proceed?[/red]"
            ):
                return

            for digest in kill:
                conn.run(f"rm -rf {shlex.quote(f'{releases_dir}/{digest}')}", warn=True)
            self.output.success("Pruning completed successfully")
