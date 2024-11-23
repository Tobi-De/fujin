from __future__ import annotations

import subprocess
from pathlib import Path

import cappa

from fujin.commands import BaseCommand
from fujin.config import InstallationMode
from fujin.connection import Connection
from fujin.secrets import resolve_secrets


@cappa.command(
    help="Deploy the project by building, transferring files, installing, and configuring services"
)
class Deploy(BaseCommand):
    def __call__(self):
        self.hook_manager.pre_build()
        parsed_env = self.parse_envfile()
        self.build_app()
        self.hook_manager.pre_deploy()
        with self.connection() as conn:
            process_manager = self.create_process_manager(conn)
            conn.run(f"mkdir -p {self.app_dir}")
            conn.run(f"mkdir -p {self.versioned_assets_dir}")
            with conn.cd(self.app_dir):
                self.transfer_files(conn, env=parsed_env)
                self.install_project(conn)
            with self.app_environment() as app_conn:
                self.release(app_conn)
                process_manager.install_services()
                process_manager.reload_configuration()
                process_manager.restart_services()
                self.create_web_proxy(app_conn).setup()
                self.update_version_history(app_conn)
                self.prune_assets(app_conn)
        self.hook_manager.post_deploy()
        self.stdout.output("[green]Project deployment completed successfully![/green]")
        self.stdout.output(
            f"[blue]Access the deployed project at: https://{self.config.host.domain_name}[/blue]"
        )

    def build_app(self) -> None:
        try:
            subprocess.run(self.config.build_command, check=True, shell=True)
        except subprocess.CalledProcessError as e:
            raise cappa.Exit(f"build command failed: {e}", code=1) from e

    @property
    def versioned_assets_dir(self) -> str:
        return f"{self.app_dir}/v{self.config.version}"

    def parse_envfile(self) -> str:
        if self.config.secret_config:
            self.stdout.output("[blue]Reading secrets....[/blue]")
            return resolve_secrets(
                self.config.host.env_content, self.config.secret_config
            )
        return self.config.host.env_content

    def transfer_files(
        self, conn: Connection, env: str, skip_requirements: bool = False
    ):
        conn.run(f"echo '{env}' > {self.app_dir}/.env")
        distfile_path = self.config.get_distfile_path()
        conn.put(
            str(distfile_path),
            f"{self.versioned_assets_dir}/{distfile_path.name}",
        )
        if not skip_requirements and self.config.requirements:
            requirements = Path(self.config.requirements)
            if not requirements.exists():
                raise cappa.Exit(f"{self.config.requirements} not found", code=1)
            conn.put(
                Path(self.config.requirements).resolve(),
                f"{self.versioned_assets_dir}/requirements.txt",
            )

    def install_project(
        self, conn: Connection, version: str | None = None, *, skip_setup: bool = False
    ):
        version = version or self.config.version
        if self.config.installation_mode == InstallationMode.PY_PACKAGE:
            self._install_python_package(conn, version, skip_setup)
        else:
            self._install_binary(conn, version)

    def _install_python_package(
        self, conn: Connection, version: str, skip_setup: bool = False
    ):
        appenv = f"""
set -a  # Automatically export all variables
source .env
set +a  # Stop automatic export
export UV_COMPILE_BYTECODE=1
export UV_PYTHON=python{self.config.python_version}
export PATH=".venv/bin:$PATH"
"""
        conn.run(f"echo '{appenv.strip()}' > {self.app_dir}/.appenv")
        versioned_assets_dir = f"{self.app_dir}/v{version}"
        if not skip_setup:
            conn.run("rm -rf .venv")
            conn.run("uv venv")
            if self.config.requirements:
                conn.run(f"uv pip install -r {versioned_assets_dir}/requirements.txt")
        conn.run(
            f"uv pip install {versioned_assets_dir}/{self.config.get_distfile_path(version).name}"
        )

    def _install_binary(self, conn: Connection, version: str):
        appenv = f"""
set -a  # Automatically export all variables
source .env
set +a  # Stop automatic export
export PATH="{self.app_dir}:$PATH"
"""
        conn.run(f"echo '{appenv.strip()}' > {self.app_dir}/.appenv")
        full_path_app_bin = f"{self.app_dir}/{self.config.app_bin}"
        conn.run(f"rm {full_path_app_bin}", warn=True)
        conn.run(
            f"ln -s {self.versioned_assets_dir}/{self.config.get_distfile_path(version).name} {full_path_app_bin}"
        )

    def release(self, conn: Connection):
        if self.config.release_command:
            conn.run(f"source .env && {self.config.release_command}")

    def update_version_history(self, conn: Connection):
        result = conn.run("head -n 1 .versions", warn=True, hide=True).stdout.strip()
        if result == self.config.version:
            return
        if result == "":
            conn.run(f"echo '{self.config.version}' > .versions")
        else:
            conn.run(f"sed -i '1i {self.config.version}' .versions")

    def prune_assets(self, conn: Connection):
        if not self.config.versions_to_keep:
            return
        result = conn.run(
            f"sed -n '{self.config.versions_to_keep + 1},$p' .versions", hide=True
        ).stdout.strip()
        result_list = result.split("\n")
        if result == "":
            return
        to_prune = [f"{self.app_dir}/v{v}" for v in result_list]
        conn.run(f"rm -r {' '.join(to_prune)}", warn=True)
        conn.run(f"sed -i '{self.config.versions_to_keep + 1},$d' .versions", warn=True)
