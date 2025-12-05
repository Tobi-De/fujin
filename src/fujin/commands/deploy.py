from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import cappa

from fujin import caddy
from fujin.commands import BaseCommand
from fujin.config import InstallationMode
from fujin.connection import SSH2Connection as Connection
from fujin.secrets import resolve_secrets

logger = logging.getLogger(__name__)


@cappa.command(
    help="Deploy the project by building, transferring files, installing, and configuring services"
)
class Deploy(BaseCommand):
    def __call__(self):
        logger.info("Starting deployment process")
        if self.config.secret_config:
            self.stdout.output("[blue]Resolving secrets from configuration...[/blue]")
            parsed_env = resolve_secrets(
                self.config.host.env_content, self.config.secret_config
            )
        else:
            parsed_env = self.config.host.env_content

        try:
            logger.debug(
                f"Building application with command: {self.config.build_command}"
            )
            self.stdout.output("[blue]Building application...[/blue]")
            subprocess.run(self.config.build_command, check=True, shell=True)
        except subprocess.CalledProcessError as e:
            raise cappa.Exit(f"build command failed: {e}", code=1) from e
        # the build commands might be responsible for creating the requirements file
        if self.config.requirements and not Path(self.config.requirements).exists():
            raise cappa.Exit(f"{self.config.requirements} not found", code=1)

        with self.connection() as conn:
            self.stdout.output("[blue]Installing project on remote host...[/blue]")
            conn.run(
                f"mkdir -p {self.config.app_dir} && echo '{parsed_env}' > {self.config.app_dir}/.env"
            )
            self.install_project(conn)
            self.stdout.output("[blue]Configuring systemd services...[/blue]")
            self.install_services(conn)
            self.restart_services(conn)
            if self.config.webserver.enabled:
                self.stdout.output("[blue]Configuring web server...[/blue]")
                caddy_configured = caddy.setup(conn, self.config)
                if not caddy_configured:
                    self.stdout.output(
                        "[red]Failed to reload Caddy.[/red]\n"
                        "[yellow]Please ensure your Caddy configuration is correct:\n"
                        "1. Directory /etc/caddy/conf.d must exist and be owned by caddy:caddy.\n"
                        "2. /etc/caddy/Caddyfile must include 'import conf.d/*.caddy' (relative path).\n"
                        "Fix these issues and rerun deploy.[/yellow]",
                    )

            # prune old versions
            with conn.cd(self.config.app_dir):
                if self.config.versions_to_keep:
                    logger.debug("Checking for old versions to prune")
                    result, _ = conn.run(
                        f"sed -n '{self.config.versions_to_keep + 1},$p' .versions",
                        hide=True,
                    )
                    result = result.strip()
                    if result:
                        result_list = result.split("\n")
                        to_prune = [f"{self.config.app_dir}/v{v}" for v in result_list]
                        if to_prune:
                            logger.debug(
                                f"Pruning old versions: {', '.join(result_list)}"
                            )
                            self.stdout.output(
                                "[blue]Pruning old release versions...[/blue]"
                            )
                            conn.run(
                                f"rm -r {' '.join(to_prune)} && sed -i '{self.config.versions_to_keep + 1},$d' .versions",
                                warn=True,
                            )
        if caddy_configured:
            self.stdout.output("[green]Deployment completed successfully![/green]")
            self.stdout.output(
                f"[blue]Application is available at: https://{self.config.host.domain_name}[/blue]"
            )

    def install_services(self, conn: Connection) -> None:
        new_units = self.config.render_systemd_units()
        for filename, content in new_units.items():
            conn.run(
                f"echo '{content}' | sudo tee /etc/systemd/system/{filename}",
                hide="out",
                pty=True,
            )

        conn.run(
            f"sudo systemctl daemon-reload && sudo systemctl enable --now {' '.join(self.config.active_systemd_units)}",
            pty=True,
        )

        valid_units = [*self.config.active_systemd_units, *(list(new_units.keys()))]

        # Cleanup Stale Instances (e.g: replicas downgrade)
        ls_units_stdout, ls_units_ok = conn.run(
            f"systemctl list-units --full --all --plain --no-legend '{self.config.app_name}*'",
            warn=True,
            hide=True,
        )
        stale_units = []
        if ls_units_ok:
            for line in ls_units_stdout.splitlines():
                unit = line.split()[0]
                if unit not in valid_units:
                    stale_units.append(unit)

        if stale_units:
            self.stdout.output(
                f"[yellow]Stopping stale service units: {', '.join(stale_units)}[/yellow]"
            )
            conn.run(f"sudo systemctl disable --now {' '.join(stale_units)}", warn=True)

        # Cleanup Stale Files & Symlinks
        stale_paths = []
        search_dirs = [
            "/etc/systemd/system",
            "/etc/systemd/system/multi-user.target.wants",
        ]

        for directory in search_dirs:
            result_stdout, result_ok = conn.run(
                f"ls {directory}/{self.config.app_name}*", warn=True, hide=True
            )
            if result_ok:
                for path in result_stdout.split():
                    filename = Path(path).name
                    if filename not in valid_units:
                        stale_paths.append(path)

        if stale_paths:
            self.stdout.output(
                f"[yellow]Cleaning up stale service files and symlinks: {', '.join([Path(p).name for p in stale_paths])}[/yellow]"
            )
            conn.run(f"sudo rm {' '.join(stale_paths)}", warn=True)

    def restart_services(self, conn: Connection) -> None:
        self.stdout.output("[blue]Restarting services...[/blue]")
        conn.run(
            f"sudo systemctl restart {' '.join(self.config.active_systemd_units)}",
            pty=True,
        )

    def install_project(
        self,
        conn: Connection,
        *,
        version: str | None = None,
        rolling_back: bool = False,
    ):
        version = version or self.config.version
        logger.debug(f"Installing project version {version}")

        # transfer binary or package file
        release_dir = self.config.get_release_dir(version)
        conn.run(f"mkdir -p {release_dir}")

        distfile_path = self.config.get_distfile_path(version)
        remote_package_path = f"{release_dir}/{distfile_path.name}"
        if not rolling_back:
            logger.debug(f"Transferring {distfile_path} to {remote_package_path}")
            conn.put(str(distfile_path), remote_package_path)

        # install project
        with conn.cd(self.config.app_dir):
            if self.config.installation_mode == InstallationMode.PY_PACKAGE:
                self._install_python_package(
                    conn,
                    remote_package_path=remote_package_path,
                    release_dir=release_dir,
                )
            else:
                self._install_binary(conn, remote_package_path)

            # run release command
            if self.config.release_command:
                logger.debug(
                    f"Executing release command: {self.config.release_command}"
                )
                self.stdout.output("[blue]Executing release command...[/blue]")
                # We use bash explicitly to ensure 'source' works and environment is preserved
                conn.run(f"bash -c 'source .appenv && {self.config.release_command}'")

            # update version history
            conn.run(
                f'current=$(head -n 1 .versions 2>/dev/null); if [ "$current" != "{version}" ]; then if [ -z "$current" ]; then echo \'{version}\' > .versions; else sed -i \'1i {version}\' .versions; fi; fi'
            )

    def _install_python_package(
        self,
        conn: Connection,
        *,
        remote_package_path: str,
        release_dir: str,
    ):
        logger.debug("Installing python package")
        appenv = f"""
set -a  # Automatically export all variables
source .env
set +a  # Stop automatic export
export UV_COMPILE_BYTECODE=1
export UV_PYTHON=python{self.config.python_version}
export PATH=".venv/bin:$PATH"
"""

        if self.config.requirements:
            local_reqs_path = Path(self.config.requirements)
            curr_release_reqs = f"{release_dir}/requirements.txt"
            conn.put(str(local_reqs_path), curr_release_reqs)

        self.stdout.output("[blue]Syncing Python dependencies...[/blue]")
        commands = [
            f"echo '{appenv.strip()}' > {self.config.app_dir}/.appenv",
            f"uv python install {self.config.python_version}",
            "test -d .venv || uv venv",
        ]
        if self.config.requirements:
            commands.append(f"uv pip install -r {release_dir}/requirements.txt")

        commands.append(f"uv pip install {remote_package_path}")
        conn.run(" && ".join(commands))

    def _install_binary(self, conn: Connection, remote_package_path: str):
        logger.debug("Installing binary")
        appenv = f"""
set -a  # Automatically export all variables
source .env
set +a  # Stop automatic export
export PATH="{self.config.app_dir}:$PATH"
"""
        full_path_app_bin = f"{self.config.app_dir}/{self.config.app_bin}"
        commands = [
            f"echo '{appenv.strip()}' > {self.config.app_dir}/.appenv",
            f"rm -f {full_path_app_bin}",
            f"ln -s {remote_package_path} {full_path_app_bin}",
        ]
        conn.run(" && ".join(commands))
