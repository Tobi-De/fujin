from __future__ import annotations

import logging
import shlex
import subprocess
import tempfile
import shutil
import hashlib
import zipapp
import json
from typing import Annotated
from pathlib import Path
import time

import importlib.util
import cappa
from rich.prompt import Confirm

from fujin.commands import BaseCommand
from fujin.secrets import resolve_secrets

logger = logging.getLogger(__name__)


@cappa.command(
    help="Deploy your application to the server",
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
            self.output.info("Resolving secrets from configuration...")
            parsed_env = resolve_secrets(
                self.config.host.env_content, self.config.secret_config
            )
        else:
            parsed_env = self.config.host.env_content

        try:
            logger.debug(
                f"Building application with command: {self.config.build_command}"
            )
            self.output.info(f"Building application v{self.config.version}...")
            subprocess.run(self.config.build_command, check=True, shell=True)
        except subprocess.CalledProcessError as e:
            raise cappa.Exit(f"build command failed: {e}", code=1) from e
        # the build commands might be responsible for creating the requirements file
        if self.config.requirements and not Path(self.config.requirements).exists():
            raise cappa.Exit(f"{self.config.requirements} not found", code=1)

        version = self.config.version
        distfile_path = self.config.get_distfile_path(version)

        with tempfile.TemporaryDirectory() as tmpdir:
            self.output.info("Preparing deployment bundle...")
            bundle_dir = Path(tmpdir) / f"{self.config.app_name}-bundle"
            bundle_dir.mkdir()

            # Copy artifacts
            shutil.copy(distfile_path, bundle_dir / distfile_path.name)
            if self.config.requirements:
                shutil.copy(self.config.requirements, bundle_dir / "requirements.txt")

            (bundle_dir / ".env").write_text(parsed_env)

            units_dir = bundle_dir / "units"
            units_dir.mkdir()
            new_units, user_units = self.config.render_systemd_units()
            for name, content in new_units.items():
                (units_dir / name).write_text(content)

            if self.config.webserver.enabled:
                (bundle_dir / "Caddyfile").write_text(self.config.render_caddyfile())

            # Create installer config
            installer_config = {
                "app_name": self.config.app_name,
                "app_dir": self.config.app_dir,
                "version": version,
                "installation_mode": self.config.installation_mode.value,
                "python_version": self.config.python_version,
                "requirements": bool(self.config.requirements),
                "distfile_name": distfile_path.name,
                "release_command": self.config.release_command,
                "webserver_enabled": self.config.webserver.enabled,
                "caddy_config_path": self.config.caddy_config_path,
                "app_bin": self.config.app_bin,
                "active_units": self.config.active_systemd_units,
                "valid_units": sorted(
                    set(self.config.active_systemd_units) | set(new_units.keys())
                ),
                "user_units": user_units,
            }

            # Create zipapp
            logger.info("Creating Python zipapp installer")
            zipapp_dir = Path(tmpdir) / "zipapp_source"
            zipapp_dir.mkdir()

            # Copy installer __main__.py
            installer_dir = (
                Path(importlib.util.find_spec("fujin").origin).parent / "_installer"
            )
            installer_src = installer_dir / "__main__.py"
            shutil.copy(installer_src, zipapp_dir / "__main__.py")

            # Copy bundle artifacts into zipapp
            for item in bundle_dir.iterdir():
                dest = zipapp_dir / item.name
                if item.is_dir():
                    shutil.copytree(item, dest)
                else:
                    shutil.copy(item, dest)

            # Write config.json
            (zipapp_dir / "config.json").write_text(
                json.dumps(installer_config, indent=2)
            )

            # Create the zipapp
            zipapp_path = Path(tmpdir) / "installer.pyz"
            zipapp.create_archive(
                zipapp_dir,
                zipapp_path,
                interpreter="/usr/bin/env python3",
            )

            # Calculate local checksum
            logger.info("Calculating local bundle checksum")
            with open(zipapp_path, "rb") as f:
                local_checksum = hashlib.file_digest(f, "sha256").hexdigest()

            remote_bundle_dir = Path(self.config.app_dir) / ".versions"
            remote_bundle_path = (
                f"{remote_bundle_dir}/{self.config.app_name}-{version}.pyz"
            )

            # Quote remote paths for shell usage (safe insertion into remote commands)
            remote_bundle_dir_q = shlex.quote(str(remote_bundle_dir))
            remote_bundle_path_q = shlex.quote(str(remote_bundle_path))

            # Upload and Execute
            with self.connection() as conn:
                conn.run(f"mkdir -p {remote_bundle_dir_q}")

                max_upload_retries = 3
                upload_ok = False
                for attempt in range(1, max_upload_retries + 1):
                    self.output.info(
                        f"Uploading deployment bundle (attempt {attempt}/{max_upload_retries})..."
                    )

                    # Upload to a temporary filename first, then move into place
                    tmp_remote = f"{remote_bundle_path}.uploading.{int(time.time())}"
                    conn.put(str(zipapp_path), tmp_remote)

                    logger.info("Verifying uploaded bundle checksum")
                    remote_checksum_out, _ = conn.run(
                        f"sha256sum {tmp_remote} | awk '{{print $1}}'",
                        hide=True,
                    )
                    remote_checksum = remote_checksum_out.strip()

                    if local_checksum == remote_checksum:
                        conn.run(f"mv {tmp_remote} {remote_bundle_path_q}")
                        upload_ok = True
                        self.output.success(
                            "Bundle uploaded and verified successfully."
                        )
                        break

                    conn.run(f"rm -f {tmp_remote}")
                    self.output.error(
                        f"Checksum mismatch! Local: {local_checksum}, Remote: {remote_checksum}"
                    )

                    if self.no_input or (
                        attempt == max_upload_retries
                        or not Confirm.ask("Upload failed. Retry?")
                    ):
                        raise cappa.Exit("Upload aborted by user.", code=1)

                if not upload_ok:
                    raise cappa.Exit("Upload failed after retries.", code=1)

                self.output.info("Executing remote installation...")
                deploy_script = f"python3 {remote_bundle_path_q} install || (echo 'install failed' >&2; exit 1)"
                if self.config.versions_to_keep:
                    deploy_script += (
                        "&& echo '==> Pruning old versions...' && "
                        f"cd {remote_bundle_dir_q} && "
                        f"ls -1t | tail -n +{self.config.versions_to_keep + 1} | xargs -r rm"
                    )
                conn.run(deploy_script, pty=True)

        self.output.success("Deployment completed successfully!")
        if self.config.webserver.enabled:
            url = f"https://{self.config.host.domain_name}"
            self.output.info(f"Application is available at: {self.output.link(url)}")
