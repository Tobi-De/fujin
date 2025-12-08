from __future__ import annotations

import logging
import shlex
import subprocess
import tarfile
import tempfile
import shutil
import hashlib
from typing import Annotated
from pathlib import Path
import time

import cappa
from rich.prompt import Confirm

from fujin.commands import BaseCommand
from fujin.commands._base import install_archive_script
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
            self.stdout.output(
                f"[blue]Building application v{self.config.version}...[/blue]"
            )
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

            context = self.config.build_context(
                distfile_name=distfile_path.name,
                user_units=user_units,
                new_units=new_units,
            )

            install_script = self.config.render_install_script(
                context=context,
            )

            (bundle_dir / "install.sh").write_text(install_script)
            logger.debug("Generated install script:\n%s", install_script)

            uninstall_script = self.config.render_uninstall_script(
                context=context,
            )
            (bundle_dir / "uninstall.sh").write_text(uninstall_script)
            logger.debug("Generated uninstall script:\n%s", uninstall_script)

            # Create tarball
            logger.info("Creating gzip-compressed deployment bundle")
            tar_ext = "tar.gz"
            tar_path = Path(tmpdir) / f"deploy.{tar_ext}"
            with tarfile.open(tar_path, "w:gz", format=tarfile.PAX_FORMAT) as tar:
                tar.add(bundle_dir, arcname=".")

            # Calculate local checksum
            logger.info("Calculating local bundle checksum")
            with open(tar_path, "rb") as f:
                local_checksum = hashlib.file_digest(f, "sha256").hexdigest()

            remote_bundle_dir = Path(self.config.app_dir) / ".versions"
            remote_bundle_path = (
                f"{remote_bundle_dir}/{self.config.app_name}-{version}.{tar_ext}"
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
                    self.stdout.output(
                        f"[blue]Uploading deployment bundle (attempt {attempt}/{max_upload_retries})...[/blue]"
                    )

                    # Upload to a temporary filename first, then move into place
                    tmp_remote = f"{remote_bundle_path}.uploading.{int(time.time())}"
                    conn.put(str(tar_path), tmp_remote)

                    logger.info("Verifying uploaded bundle checksum")
                    remote_checksum_out, _ = conn.run(
                        f"sha256sum {tmp_remote} | awk '{{print $1}}'",
                        hide=True,
                    )
                    remote_checksum = remote_checksum_out.strip()

                    if local_checksum == remote_checksum:
                        conn.run(f"mv {tmp_remote} {remote_bundle_path_q}")
                        upload_ok = True
                        self.stdout.output(
                            "[green]Bundle uploaded and verified successfully.[/green]"
                        )
                        break

                    conn.run(f"rm -f {tmp_remote}")
                    self.stdout.output(
                        f"[red]Checksum mismatch! Local: {local_checksum}, Remote: {remote_checksum}[/red]"
                    )

                    if self.no_input or (
                        attempt == max_upload_retries
                        or not Confirm.ask("Upload failed. Retry?")
                    ):
                        raise cappa.Exit("Upload aborted by user.", code=1)

                if not upload_ok:
                    raise cappa.Exit("Upload failed after retries.", code=1)

                self.stdout.output("[blue]Executing remote installation...[/blue]")
                deploy_script = install_archive_script(
                    remote_bundle_path_q,
                    app_name=self.config.app_name,
                    version=version,
                )
                if self.config.versions_to_keep:
                    deploy_script += (
                        "&& echo '==> Pruning old versions...' && "
                        f"cd {remote_bundle_dir_q} && "
                        f"ls -1t | tail -n +{self.config.versions_to_keep + 1} | xargs -r rm"
                    )
                conn.run(deploy_script, pty=True)

        self.stdout.output("[green]Deployment completed successfully![/green]")
        if self.config.webserver.enabled:
            self.stdout.output(
                f"[blue]Application is available at: https://{self.config.host.domain_name}[/blue]"
            )
