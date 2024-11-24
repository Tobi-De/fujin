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
        with self.connection() as conn, conn.cd(self.app_dir):
            result = conn.run(
                f"sed -n '{self.keep + 1},$p' .versions", hide=True
            ).stdout.strip()
            result_list = result.split("\n")
            if result == "":
                self.stdout.output("[blue]No versions to prune[/blue]")
                return
            if not Confirm.ask(
                f"[red]The following versions will be permanently deleted: {', '.join(result_list)}. This action is irreversible. Are you sure you want to proceed?[/red]"
            ):
                return
            to_prune = [f"{self.app_dir}/v{v}" for v in result_list]
            conn.run(f"rm -r {' '.join(to_prune)}", warn=True)
            conn.run(f"sed -i '{self.keep + 1},$d' .versions", warn=True)
            self.stdout.output("[green]Pruning completed successfully[/green]")
