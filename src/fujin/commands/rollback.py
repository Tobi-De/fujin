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
        with self.connection() as conn:
            result, _ = conn.run(
                f"ls -1t {self.config.app_dir}/.versions", warn=True, hide=True
            )
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
                f"cat {self.config.app_dir}/.current_version", warn=True, hide=True
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
                current_bundle = f"{self.config.app_dir}/.versions/{self.config.app_name}-{current_version}.tar.gz"

                # Check if bundle exists
                _, exists = conn.run(f"test -f {current_bundle}", warn=True, hide=True)
                if exists:
                    # Extract and run uninstall.sh
                    # We extract to a temp dir to be safe
                    tmp_uninstall_dir = f"/tmp/uninstall-{current_version}"
                    conn.run(f"mkdir -p {tmp_uninstall_dir}")
                    # Extract full bundle to ensure we get the script regardless of pathing
                    conn.run(f"tar -xzf {current_bundle} -C {tmp_uninstall_dir}")
                    if conn.run(f"test -f {tmp_uninstall_dir}/uninstall.sh", warn=True)[
                        1
                    ]:
                        conn.run(f"bash {tmp_uninstall_dir}/uninstall.sh", warn=True)
                    else:
                        self.stdout.output(
                            f"[yellow]Warning: uninstall.sh not found in bundle for version {current_version}.[/yellow]"
                        )

                    conn.run(f"rm -rf {tmp_uninstall_dir}")
                else:
                    self.stdout.output(
                        f"[yellow]Bundle for current version {current_version} not found. Skipping uninstall.[/yellow]"
                    )

            # Install target
            self.stdout.output(f"[blue]Installing version {version}...[/blue]")
            target_bundle = f"{self.config.app_dir}/.versions/{self.config.app_name}-{version}.tar.gz"
            remote_extract_dir = f"/tmp/{self.config.app_name}-{version}"

            install_cmd = (
                f"mkdir -p {remote_extract_dir} && "
                f"tar -xzf {target_bundle} -C {remote_extract_dir} && "
                f"cd {remote_extract_dir} && "
                f"bash install.sh && "
                f"cd / && rm -rf {remote_extract_dir}"
            )
            conn.run(install_cmd, pty=True)
            self.stdout.output(
                f"[green]Rollback to version {version} completed successfully![/green]"
            )
