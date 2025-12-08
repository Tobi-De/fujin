from dataclasses import dataclass
import shlex

import cappa
from rich.prompt import Confirm
from rich.prompt import Prompt

from fujin.commands import BaseCommand, install_archive_script, uninstall_archive_script


@cappa.command(help="Rollback application to a previous version")
@dataclass
class Rollback(BaseCommand):
    def __call__(self):
        with self.connection() as conn:
            app_dir = shlex.quote(self.config.app_dir)
            result, _ = conn.run(f"ls -1t {app_dir}/.versions", warn=True, hide=True)
            if not result:
                self.stdout.output("[blue]No rollback targets available")
                return

            filenames = result.strip().splitlines()
            versions = []
            prefix = f"{self.config.app_name}-"
            suffix = ".tar.gz"
            for fname in filenames:
                if fname.startswith(prefix) and fname.endswith(suffix):
                    v = fname[len(prefix) : -len(suffix)]
                    versions.append(v)

            if not versions:
                self.stdout.output("[blue]No rollback targets available")
                return

            try:
                version = Prompt.ask(
                    "Enter the version you want to rollback to:",
                    choices=versions,
                    default=versions[0] if versions else None,
                )
            except KeyboardInterrupt as e:
                raise cappa.Exit("Rollback aborted by user.", code=0) from e

            current_version, _ = conn.run(
                f"cat {app_dir}/.version", warn=True, hide=True
            )
            current_version = current_version.strip()

            if current_version == version:
                self.stdout.output(
                    f"[yellow]Version {version} is already the current version.[/yellow]"
                )
                return

            confirm = Confirm.ask(
                f"[blue]Rolling back from v{current_version} to v{version}. Are you sure you want to proceed?[/blue]"
            )
            if not confirm:
                return

            # Uninstall current
            if current_version:
                self.stdout.output(
                    f"[blue]Uninstalling current version {current_version}...[/blue]"
                )
                current_bundle = f"{app_dir}/.versions/{self.config.app_name}-{current_version}.tar.gz"

                # Check if bundle exists
                _, exists = conn.run(f"test -f {current_bundle}", warn=True, hide=True)
                if exists:
                    uninstall_cmd = uninstall_archive_script(
                        current_bundle, self.config.app_name, current_version
                    )
                    _, ok = conn.run(uninstall_cmd, warn=True)
                    if not ok:
                        self.stdout.output(
                            f"[yellow]Warning: uninstall failed for version {current_version}.[/yellow]"
                        )
                else:
                    self.stdout.output(
                        f"[yellow]Bundle for current version {current_version} not found. Skipping uninstall.[/yellow]"
                    )

            # Install target
            self.stdout.output(f"[blue]Installing version {version}...[/blue]")
            target_bundle = (
                f"{app_dir}/.versions/{self.config.app_name}-{version}.tar.gz"
            )
            install_cmd = install_archive_script(
                target_bundle, self.config.app_name, version
            )
            # delete all versions after new target
            cleanup_cmd = (
                f"cd {app_dir}/.versions && ls -1t | "
                f"awk '/{self.config.app_name}-{version}\\.tar\\.gz/{{exit}} {{print}}' | "
                "xargs -r rm"
            )
            full_cmd = install_cmd + (
                f" && echo '==> Cleaning up newer versions...' && {cleanup_cmd}"
            )
            conn.run(full_cmd, pty=True)
            self.stdout.output(
                f"[green]Rollback to version {version} completed successfully![/green]"
            )
