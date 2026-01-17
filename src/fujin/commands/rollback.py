from dataclasses import dataclass
import shlex

import cappa
from rich.prompt import Confirm
from rich.prompt import Prompt

from fujin.commands import BaseCommand
from fujin.audit import log_operation


@cappa.command(
    help="Roll back application to a previous version",
)
@dataclass
class Rollback(BaseCommand):
    def __call__(self):
        with self.connection() as conn:
            shlex.quote(self.config.app_dir)
            fujin_dir = shlex.quote(self.config.install_dir)
            result, _ = conn.run(f"ls -1t {fujin_dir}/.versions", warn=True, hide=True)
            if not result:
                self.output.info("No rollback targets available")
                return

            filenames = result.strip().splitlines()
            versions = []
            prefix = f"{self.config.app_name}-"
            for fname in filenames:
                if fname.startswith(prefix) and fname.endswith(".pyz"):
                    v = fname[len(prefix) : -4]
                    versions.append(v)

            if not versions:
                self.output.info("No rollback targets available")
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
                f"cat {fujin_dir}/.version", warn=True, hide=True
            )
            current_version = current_version.strip()

            if current_version == version:
                self.output.warning(
                    f"Version {version} is already the current version."
                )
                return

            confirm = Confirm.ask(
                f"[blue]Rolling back from v{current_version} to v{version}. Are you sure you want to proceed?[/blue]"
            )
            if not confirm:
                return

            # Uninstall current
            if current_version:
                self.output.info(f"Uninstalling current version {current_version}...")
                current_bundle = f"{fujin_dir}/.versions/{self.config.app_name}-{current_version}.pyz"
                _, exists = conn.run(f"test -f {current_bundle}", warn=True, hide=True)

                if exists:
                    uninstall_cmd = f"python3 {current_bundle} uninstall"
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
            install_cmd = f"python3 {target_bundle} install || (echo 'install failed' >&2; exit 1)"

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
