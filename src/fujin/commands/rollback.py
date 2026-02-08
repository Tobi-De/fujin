from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Annotated

import cappa
from rich.console import Console
from rich.prompt import Confirm, IntPrompt

from fujin.audit import log_operation
from fujin.commands import BaseCommand


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
        with self.connection() as conn:
            shlex.quote(self.config.app_dir)
            fujin_dir = shlex.quote(self.config.install_dir)
            result, _ = conn.run(
                f"cat {fujin_dir}/.version 2>/dev/null; echo '---'; ls -1t {fujin_dir}/.versions",
                warn=True,
                hide=True,
            )

            parts = result.split("---\n", 1)
            current_version = parts[0].strip()
            filenames = parts[1].strip().splitlines() if len(parts) > 1 else []

            versions = []
            prefix = f"{self.config.app_name}-"
            for fname in filenames:
                if fname.startswith(prefix) and fname.endswith(".pyz"):
                    v = fname[len(prefix) : -4]
                    versions.append(v)

            # Filter out current version from choices
            available_versions = [v for v in versions if v != current_version]

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

            # Uninstall current
            if current_version:
                self.output.info(f"Uninstalling current version {current_version}...")
                current_bundle = f"{fujin_dir}/.versions/{self.config.app_name}-{current_version}.pyz"
                _, exists = conn.run(f"test -f {current_bundle}", warn=True, hide=True)

                if exists:
                    verbose_flag = (
                        f" --verbose {self.verbose}" if self.verbose > 0 else ""
                    )
                    uninstall_cmd = (
                        f"sudo python3 {current_bundle} uninstall{verbose_flag}"
                    )
                    _, ok = conn.run(uninstall_cmd, warn=True)
                    if not ok:
                        self.output.warning(
                            f"Warning: uninstall failed for version {current_version}."
                        )
                else:
                    self.output.warning(
                        f"Bundle for current version {current_version} not found. Skipping uninstall."
                    )

            # Install target
            self.output.info(f"Installing version {version}...")
            target_bundle = (
                f"{fujin_dir}/.versions/{self.config.app_name}-{version}.pyz"
            )
            verbose_flag = f" --verbose {self.verbose}" if self.verbose > 0 else ""
            install_cmd = f"sudo python3 {target_bundle} install{verbose_flag} || (echo 'install failed' >&2; exit 1)"

            # delete all versions after new target
            cleanup_cmd = (
                f"cd {fujin_dir}/.versions && ls -1t | "
                f"awk '/{self.config.app_name}-{version}\\.pyz/{{exit}} {{print}}' | "
                "xargs -r rm"
            )
            full_cmd = install_cmd + (
                f" && echo '==> Cleaning up newer versions...' && {cleanup_cmd}"
            )
            conn.run(full_cmd, pty=True)

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
