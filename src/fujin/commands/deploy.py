from __future__ import annotations

import logging
import subprocess
import tarfile
import tempfile
import shutil
import hashlib
from typing import Annotated
from pathlib import Path

import cappa
from rich.prompt import Confirm

from fujin.commands import BaseCommand
from fujin.config import InstallationMode
from fujin.secrets import resolve_secrets

logger = logging.getLogger(__name__)


@cappa.command(
    help="Deploy the project by building, transferring files, installing, and configuring services"
)
class Deploy(BaseCommand):
    no_input: Annotated[
        bool,
        cappa.Arg(
            long="--no-input",
            help="Do not prompt for input (e.g. retry upload)",
        ),
    ] = False

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

        version = self.config.version
        distfile_path = self.config.get_distfile_path(version)

        with tempfile.TemporaryDirectory() as tmpdir:
            self.stdout.output("[blue]Preparing deployment bundle...[/blue]")
            bundle_dir = Path(tmpdir) / "bundle"
            bundle_dir.mkdir()

            # Copy artifacts
            shutil.copy(distfile_path, bundle_dir / distfile_path.name)
            if self.config.requirements:
                shutil.copy(self.config.requirements, bundle_dir / "requirements.txt")

            # Write .env
            (bundle_dir / ".env").write_text(parsed_env)

            # Write systemd units
            units_dir = bundle_dir / "units"
            units_dir.mkdir()
            new_units = self.config.render_systemd_units()
            for name, content in new_units.items():
                (units_dir / name).write_text(content)

            # Write Caddyfile
            if self.config.webserver.enabled:
                (bundle_dir / "Caddyfile").write_text(self.config.render_caddyfile())

            # Generate scripts
            install_script = self._generate_install_script(
                version=version,
                distfile_name=distfile_path.name,
                new_units=new_units,
            )
            (bundle_dir / "install.sh").write_text(install_script)

            uninstall_script = self._generate_uninstall_script(new_units)
            (bundle_dir / "uninstall.sh").write_text(uninstall_script)

            # Create tarball
            tar_path = Path(tmpdir) / "deploy.tar.gz"
            with tarfile.open(tar_path, "w:gz") as tar:
                tar.add(bundle_dir, arcname=".")

            # Calculate local checksum
            sha256_hash = hashlib.sha256()
            with open(tar_path, "rb") as f:
                for byte_block in iter(lambda: f.read(4096), b""):
                    sha256_hash.update(byte_block)
            local_checksum = sha256_hash.hexdigest()

            # Upload and Execute
            with self.connection() as conn:
                bundle_filename = f"{self.config.app_name}-{version}.tar.gz"
                remote_bundle_dir = f"{self.config.app_dir}/.versions"
                remote_bundle_path = f"{remote_bundle_dir}/{bundle_filename}"

                conn.run(f"mkdir -p {remote_bundle_dir}")

                while True:
                    self.stdout.output("[blue]Uploading deployment bundle...[/blue]")
                    conn.put(str(tar_path), remote_bundle_path)

                    # Verify checksum
                    remote_checksum_out, _ = conn.run(
                        f"sha256sum {remote_bundle_path} | awk '{{print $1}}'",
                        hide=True,
                    )
                    remote_checksum = remote_checksum_out.strip()

                    if local_checksum == remote_checksum:
                        self.stdout.output(
                            "[green]Bundle uploaded and verified successfully.[/green]"
                        )
                        break

                    self.stdout.output(
                        f"[red]Checksum mismatch! Local: {local_checksum}, Remote: {remote_checksum}[/red]"
                    )

                    if self.no_input:
                        raise cappa.Exit("Upload failed: Checksum mismatch.", code=1)

                    if not Confirm.ask("Upload failed. Retry?"):
                        raise cappa.Exit("Upload aborted by user.", code=1)

                self.stdout.output("[blue]Executing remote installation...[/blue]")
                remote_extract_dir = f"/tmp/{self.config.app_name}-{version}"

                install_cmd = (
                    f"mkdir -p {remote_extract_dir} && "
                    f"tar -xzf {remote_bundle_path} -C {remote_extract_dir} && "
                    f"cd {remote_extract_dir} && "
                    f"bash install.sh && "
                    f"cd / && rm -rf {remote_extract_dir}"
                )
                conn.run(install_cmd, pty=True)

        self.stdout.output("[green]Deployment completed successfully![/green]")
        if self.config.webserver.enabled:
            self.stdout.output(
                f"[blue]Application is available at: https://{self.config.host.domain_name}[/blue]"
            )

    def _generate_install_script(
        self,
        version: str,
        distfile_name: str,
        new_units: dict[str, str],
    ) -> str:
        script = ["#!/usr/bin/env bash", "set -e"]
        script.append("BUNDLE_DIR=$(pwd)")

        script.append('echo "Setting up directories..."')
        # Setup directories
        script.append(f"mkdir -p {self.config.app_dir}")
        script.append(f"mv .env {self.config.app_dir}/.env")

        # Install Project
        script.append(f"cd {self.config.app_dir}")

        if self.config.installation_mode == InstallationMode.PY_PACKAGE:
            script.extend(
                self._get_python_package_install_commands(
                    distfile_path=f"$BUNDLE_DIR/{distfile_name}",
                    requirements_path=(
                        f"$BUNDLE_DIR/requirements.txt"
                        if self.config.requirements
                        else None
                    ),
                )
            )
        else:
            script.extend(
                self._get_binary_install_commands(
                    distfile_path=f"$BUNDLE_DIR/{distfile_name}"
                )
            )

        # Release Command
        if self.config.release_command:
            script.append(f"echo 'Running release command'")
            script.append(f"bash -c 'source .appenv && {self.config.release_command}'")

        # Version Management
        script.append(f"echo '{version}' > .current_version")

        script.append("cd $BUNDLE_DIR")

        script.append('echo "Configuring systemd services..."')
        # Install Services
        for name in new_units.keys():
            script.append(f"sudo cp units/{name} /etc/systemd/system/")

        script.append("sudo systemctl daemon-reload")

        # Cleanup Stale Units & Files
        valid_units = set(self.config.active_systemd_units) | set(new_units.keys())
        valid_units_str = " ".join(valid_units)
        script.append(
            f"""
# Cleanup Stale Units
VALID_UNITS="{valid_units_str}"
ALL_UNITS=$(systemctl list-units --full --all --plain --no-legend '{self.config.app_name}*' | awk '{{print $1}}')
for UNIT in $ALL_UNITS; do
    if [[ ! " $VALID_UNITS " =~ " $UNIT " ]]; then
        echo "Stopping stale service unit: $UNIT"
        sudo systemctl disable --now "$UNIT" || true
    fi
done

# Cleanup Stale Files
SEARCH_DIRS="/etc/systemd/system /etc/systemd/system/multi-user.target.wants"
for DIR in $SEARCH_DIRS; do
    if [ -d "$DIR" ]; then
        FILES=$(ls $DIR/{self.config.app_name}* 2>/dev/null || true)
        for FILE in $FILES; do
            BASENAME=$(basename "$FILE")
            if [[ ! " $VALID_UNITS " =~ " $BASENAME " ]]; then
                echo "Removing stale service file: $FILE"
                sudo rm -f "$FILE"
            fi
        done
    fi
done
sudo systemctl daemon-reload
"""
        )

        script.append('echo "Restarting services..."')
        script.append(
            f"sudo systemctl enable {' '.join(self.config.active_systemd_units)}"
        )
        script.append(
            f"sudo systemctl restart {' '.join(self.config.active_systemd_units)}"
        )

        # Caddy
        if self.config.webserver.enabled:
            script.append('echo "Configuring Caddy..."')
            script.append(f"sudo mkdir -p $(dirname {self.config.caddy_config_path})")
            script.append(f"sudo mv Caddyfile {self.config.caddy_config_path}")
            script.append(f"sudo chown caddy:caddy {self.config.caddy_config_path}")
            script.append("sudo systemctl reload caddy")

        # Prune
        if self.config.versions_to_keep:
            script.append('echo "Pruning old versions..."')
            script.append(f"cd {self.config.app_dir}/.versions")
            # Keep only the N most recent bundles
            script.append(
                f"ls -1t | tail -n +{self.config.versions_to_keep + 1} | xargs -r rm"
            )

        return "\n".join(script)

    def _generate_uninstall_script(self, new_units: dict[str, str]) -> str:
        script = ["#!/usr/bin/env bash", "set -e"]

        # Stop and disable services
        units = " ".join(self.config.active_systemd_units)
        if units:
            script.append(f"sudo systemctl disable --now {units}")

        # Remove units
        for name in new_units.keys():
            script.append(f"sudo rm -f /etc/systemd/system/{name}")

        script.append("sudo systemctl daemon-reload")
        script.append("sudo systemctl reset-failed")

        # Remove Caddy config
        if self.config.webserver.enabled:
            script.append(f"sudo rm -f {self.config.caddy_config_path}")
            script.append("sudo systemctl reload caddy")

        return "\n".join(script)

    def _get_python_package_install_commands(
        self,
        *,
        distfile_path: str,
        requirements_path: str | None,
    ) -> list[str]:
        logger.debug("Installing python package")
        appenv = f"""
set -a  # Automatically export all variables
source .env
set +a  # Stop automatic export
export UV_COMPILE_BYTECODE=1
export UV_PYTHON=python{self.config.python_version}
export PATH=".venv/bin:$PATH"
"""
        commands = [
            f"echo '{appenv.strip()}' > {self.config.app_dir}/.appenv",
            "echo 'Syncing Python dependencies...'",
            f"uv python install {self.config.python_version}",
            "test -d .venv || uv venv",
        ]
        if requirements_path:
            commands.append(f"uv pip install -r {requirements_path}")
        commands.append(f"uv pip install {distfile_path}")
        return commands

    def _get_binary_install_commands(self, distfile_path: str) -> list[str]:
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
            "echo 'Installing binary...'",
            f"cp {distfile_path} {full_path_app_bin}",
            f"chmod +x {full_path_app_bin}",
        ]
        return commands
