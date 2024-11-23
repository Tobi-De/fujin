from __future__ import annotations

import hashlib
from pathlib import Path

import cappa

from fujin.commands import BaseCommand
from fujin.config import InstallationMode
from fujin.connection import Connection
from .deploy import Deploy


@cappa.command(help="Redeploy the application to apply code and environment changes")
class Redeploy(BaseCommand):
    def __call__(self):
        deploy = Deploy()
        self.hook_manager.pre_build()
        parsed_env = deploy.parse_envfile()
        deploy.build_app()
        self.hook_manager.pre_deploy()
        with self.app_environment() as conn:
            conn.run(f"mkdir -p {deploy.versioned_assets_dir}")
            requirements_copied = self._copy_requirements_if_needed(conn)
            deploy.transfer_files(
                conn, env=parsed_env, skip_requirements=requirements_copied
            )
            deploy.install_project(conn, skip_setup=requirements_copied)
            deploy.release(conn)
            self.create_process_manager(conn).restart_services()
            deploy.update_version_history(conn)
        self.hook_manager.post_deploy()
        self.stdout.output("[green]Redeployment completed successfully![/green]")

    def _copy_requirements_if_needed(self, conn: Connection) -> bool:
        if (
            not self.config.requirements
            or self.config.installation_mode == InstallationMode.BINARY
        ):
            return False
        local_requirements = hashlib.md5(
            Path(self.config.requirements).read_bytes()
        ).hexdigest()
        current_host_version = conn.run(
            "head -n 1 .versions", warn=True, hide=True
        ).stdout.strip()
        try:
            host_requirements = (
                conn.run(
                    f"md5sum v{current_host_version}/requirements.txt",
                    warn=True,
                    hide=True,
                )
                .stdout.strip()
                .split()[0]
            )
            skip_requirements = host_requirements == local_requirements
        except IndexError:
            return False
        if skip_requirements and current_host_version != self.config.version:
            conn.run(
                f"cp v{current_host_version}/requirements.txt  {self.app_dir}/v{self.config.version}/requirements.txt "
            )
        return True
