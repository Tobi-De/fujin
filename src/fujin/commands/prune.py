from dataclasses import dataclass
from typing import Annotated

import cappa
from rich.prompt import Confirm

from fujin.commands import BaseCommand


@cappa.command(
    help="Prune old artifacts, keeping only the specified number of recent versions"
)
@dataclass
class Prune(BaseCommand):
    keep: Annotated[
        int,
        cappa.Arg(
            short="-k",
            long="--keep",
            help="Number of version artifacts to retain (minimum 1)",
        ),
    ] = 2

    def __call__(self):
        if self.keep < 1:
            raise cappa.Exit("The minimum value for the --keep option is 1", code=1)

        versions_dir = f"{self.config.app_dir}/.versions"
        with self.connection() as conn:
            _, success = conn.run(f"test -d {versions_dir}", warn=True, hide=True)
            if not success:
                self.stdout.output(
                    "[blue]No versions directory found. Nothing to prune.[/blue]"
                )
                return

            # List files sorted by time (newest first)
            result, _ = conn.run(f"ls -1t {versions_dir}", warn=True, hide=True)

            if not result:
                self.stdout.output("[blue]No versions found to prune[/blue]")
                return

            filenames = result.strip().splitlines()
            prefix = f"{self.config.app_name}-"
            suffix = ".tar.gz"

            valid_bundles = []
            for fname in filenames:
                if fname.startswith(prefix) and fname.endswith(suffix):
                    valid_bundles.append(fname)

            if len(valid_bundles) <= self.keep:
                self.stdout.output(
                    f"[blue]Only {len(valid_bundles)} versions found. Nothing to prune (keep={self.keep}).[/blue]"
                )
                return

            to_delete = valid_bundles[self.keep :]
            # Extract versions for display
            versions_to_delete = []
            for fname in to_delete:
                v = fname[len(prefix) : -len(suffix)]
                versions_to_delete.append(v)

            if not Confirm.ask(
                f"[red]The following versions will be permanently deleted: {', '.join(versions_to_delete)}.\\n"
                f"This action is irreversible. Are you sure you want to proceed?[/red]"
            ):
                return

            cmd = f"cd {versions_dir} && rm -f {' '.join(to_delete)}"
            conn.run(cmd)
            self.stdout.output("[green]Pruning completed successfully[/green]")
