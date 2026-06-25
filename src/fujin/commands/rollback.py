from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Annotated

import cappa
from rich.console import Console
from rich.prompt import Confirm, IntPrompt

from fujin.audit import log_operation
from fujin.commands import BaseCommand
from fujin import connection


@cappa.command(
    help="Roll back application to a previous version",
)
@dataclass
class Rollback(BaseCommand):
    previous: Annotated[
        bool,
        cappa.Arg(
            long="--previous",
            short="-p",
            help="Automatically roll back to the most recent previous version without prompting",
        ),
    ] = False
    strict: Annotated[
        bool,
        cappa.Arg(
            long="--strict",
            short="-s",
            help="Exit with an error if no rollback targets are available",
        ),
    ] = False

    def __call__(self):
        with connection.connection(host=self.selected_host) as conn:
            app_dir_q = shlex.quote(self.config.app_dir)
            releases_dir_q = shlex.quote(f"{self.config.app_dir}/releases")

            # Read current version from symlink target
            result, _ = conn.run(
                f"readlink {app_dir_q}/current 2>/dev/null | xargs basename || echo ''; "
                f"echo '---'; ls -1t {releases_dir_q} 2>/dev/null || true",
                warn=True,
                hide=True,
            )

            parts = result.split("---\n", 1)
            current_version = parts[0].strip()
            dirnames = parts[1].strip().splitlines() if len(parts) > 1 else []

            # All directory names in releases/ are version identifiers
            available_versions = [d for d in dirnames if d and d != current_version]

            if not available_versions:
                msg = "No previous versions available for rollback"
                if self.strict:
                    raise cappa.Exit(msg, code=1)
                return self.output.info(msg)

            if self.previous:
                version = available_versions[0]
                self.output.info(f"Rolling back from {current_version} to {version}...")
            else:
                console = Console()
                console.print(f"\n[bold]Current version:[/bold] {current_version}\n")
                console.print("[bold]Available versions:[/bold]")
                for i, v in enumerate(available_versions, 1):
                    console.print(f"  [cyan]{i}[/cyan]. {v}")
                console.print()

                try:
                    choice = IntPrompt.ask(
                        "Select version number",
                        default=1,
                    )
                    if choice < 1 or choice > len(available_versions):
                        self.output.error(
                            f"Invalid choice. Please enter a number between 1 and {len(available_versions)}"
                        )
                        return
                    version = available_versions[choice - 1]
                except KeyboardInterrupt as e:
                    raise cappa.Exit("\nRollback aborted by user.", code=0) from e

                confirm = Confirm.ask(
                    f"\n[bold yellow]Roll back to {version}?[/bold yellow]"
                )
                if not confirm:
                    return

            # Swap symlink to the selected release
            release_dir_q = shlex.quote(f"{self.config.app_dir}/releases/{version}")
            _, release_exists = conn.run(
                f"test -d {release_dir_q}", warn=True, hide=True
            )

            if not release_exists:
                self.output.error(
                    f"Release directory for version {version} not found. "
                    "It may have been pruned. Re-deploy the desired version instead."
                )
                return

            self.output.info(f"Switching to release {version}...")
            conn.run(
                f"ln -sfn {release_dir_q} {app_dir_q}/.current.tmp && "
                f"mv {app_dir_q}/.current.tmp {app_dir_q}/current",
            )
            conn.run(
                f"systemctl daemon-reload && "
                f"systemctl restart {self.config.app_name}-*.service",
                pty=True,
            )

            # Remove releases newer than the target (they were deployed after it)
            conn.run(
                f"cd {releases_dir_q} && ls -1t | "
                f"awk '/{version}/{{exit}} {{print}}' | "
                "xargs -r rm -rf",
                warn=True,
            )

            log_operation(
                connection=conn,
                app_name=self.config.app_name,
                operation="rollback",
                host=self.selected_host.name or self.selected_host.address,
                from_version=current_version,
                to_version=version,
            )

        self.output.success(f"Rollback to version {version} completed successfully!")
        return 1
