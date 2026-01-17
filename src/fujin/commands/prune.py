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
        int | None,
        cappa.Arg(
            short="-k",
            long="--keep",
            help="Number of version artifacts to retain (minimum 1). Defaults to versions_to_keep from config",
        ),
    ] = None

    def __call__(self):
        # Use config value if --keep not specified
        keep = (
            self.keep if self.keep is not None else (self.config.versions_to_keep or 5)
        )

        if keep < 1:
            raise cappa.Exit("The minimum value for the --keep option is 1", code=1)

        versions_dir = f"{self.config.install_dir}/.versions"
        with self.connection() as conn:
            _, success = conn.run(f"test -d {versions_dir}", warn=True, hide=True)
            if not success:
                self.output.info("No versions directory found. Nothing to prune.")
                return

            # List files sorted by time (newest first)
            result, _ = conn.run(f"ls -1t {versions_dir}", warn=True, hide=True)

            if not result:
                self.output.info("No versions found to prune")
                return

            filenames = result.strip().splitlines()
            prefix = f"{self.config.app_name}-"
            suffix = ".pyz"

            valid_bundles = []
            for fname in filenames:
                if fname.startswith(prefix) and fname.endswith(suffix):
                    valid_bundles.append(fname)

            if len(valid_bundles) <= keep:
                self.output.info(
                    f"Only {len(valid_bundles)} versions found. Nothing to prune (keep={keep})."
                )
                return

            to_delete = valid_bundles[keep:]
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
            self.output.success("Pruning completed successfully")
