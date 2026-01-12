from __future__ import annotations
import hashlib
import importlib.util
import json
import logging
import shlex
import shutil
import subprocess
import tempfile
import time
import zipapp
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from fujin.discovery import ServiceUnit
import configparser
import cappa
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm
from rich.table import Table

from fujin.audit import log_operation
from fujin.commands import BaseCommand
from fujin.errors import BuildError, DeploymentError
from fujin.errors import UploadError
from fujin.secrets import resolve_secrets
from fujin.formatting import safe_format

logger = logging.getLogger(__name__)


@cappa.command(
    help="Deploy your application to the server",
)
@dataclass
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
                self.selected_host.env_content, self.config.secret_config
            )
        else:
            parsed_env = self.selected_host.env_content

        try:
            logger.debug(
                f"Building application with command: {self.config.build_command}"
            )
            self.output.info(f"Building application ...")
            subprocess.run(self.config.build_command, check=True, shell=True)
        except subprocess.CalledProcessError as e:
            self.output.error(f"Build command failed with exit code {e.returncode}")
            self.output.info(
                f"Command: {self.config.build_command}\n\n"
                "Troubleshooting:\n"
                "  - Check that all build dependencies are installed\n"
                "  - Verify your build_command in fujin.toml is correct\n"
                "  - Try running the build command manually to see full error output"
            )
            raise BuildError("Build failed", command=self.config.build_command) from e
        # the build commands might be responsible for creating the requirements file
        if self.config.requirements and not Path(self.config.requirements).exists():
            self.output.error(
                f"Requirements file not found: {self.config.requirements}"
            )
            self.output.info(
                "\nTroubleshooting:\n"
                "  - Ensure your build_command generates the requirements file\n"
                "  - Check that the 'requirements' path in fujin.toml is correct\n"
                f"  - Try running: uv pip compile pyproject.toml -o {self.config.requirements}"
            )
            raise BuildError(f"Requirements file not found: {self.config.requirements}")

        version = self.config.version
        distfile_path = self.config.get_distfile_path(version)

        discovered_services = self.config.discovered_services
        if not discovered_services:
            raise DeploymentError("No systemd units found, nothing to deploy")

        with tempfile.TemporaryDirectory() as tmpdir:
            self.output.info("Preparing deployment bundle...")
            bundle_dir = Path(tmpdir) / f"{self.config.app_name}-bundle"
            bundle_dir.mkdir()

            # Copy artifacts
            shutil.copy(distfile_path, bundle_dir / distfile_path.name)
            if self.config.requirements:
                shutil.copy(self.config.requirements, bundle_dir / "requirements.txt")

            (bundle_dir / ".env").write_text(parsed_env)

            # Discover and resolve systemd units locally
            context = {
                "app_name": self.config.app_name,
                "version": version,
                "app_dir": self.config.app_dir(self.selected_host),
                "user": self.selected_host.user,
            }

            unit_metadata = []
            logger.debug("Validating and resolving systemd units")
            systemd_bundle = bundle_dir / "systemd"
            systemd_bundle.mkdir()

            # Process each service and build metadata
            for svc in discovered_services:
                # Validate and resolve main service file
                service_content = svc.service_file.read_text()
                # Use safe_format for everything to handle both legacy (double brace)
                # and new (single brace) templates, while correctly ignoring non-matching
                # braces in Caddyfiles.
                resolved_content = safe_format(service_content, **context)

                # Validate with configparser
                parser = configparser.ConfigParser(strict=False, allow_no_value=True)
                try:
                    parser.read_string(resolved_content, source=str(svc.service_file))
                except Exception as e:
                    raise BuildError(
                        f"Unit validation failed: {svc.service_file.name}"
                    ) from e

                (systemd_bundle / svc.service_file.name).write_text(resolved_content)

                # Build metadata entry
                metadata = {
                    "name": svc.name,
                    "is_template": svc.is_template,
                    "service_file": svc.service_file.name,
                    "deployed_service": f"{self.config.app_name}-{svc.name}{'@' if svc.is_template else ''}.service",
                }

                # Process and add socket file if exists
                if svc.socket_file:
                    socket_content = svc.socket_file.read_text()
                    resolved_socket = safe_format(socket_content, **context)
                    (systemd_bundle / svc.socket_file.name).write_text(resolved_socket)
                    metadata["socket_file"] = svc.socket_file.name
                    metadata["deployed_socket"] = (
                        f"{self.config.app_name}-{svc.name}{'@' if svc.is_template else ''}.socket"
                    )

                # Process and add timer file if exists
                if svc.timer_file:
                    timer_content = svc.timer_file.read_text()
                    resolved_timer = safe_format(timer_content, **context)
                    (systemd_bundle / svc.timer_file.name).write_text(resolved_timer)
                    metadata["timer_file"] = svc.timer_file.name
                    metadata["deployed_timer"] = (
                        f"{self.config.app_name}-{svc.name}{'@' if svc.is_template else ''}.timer"
                    )

                unit_metadata.append(metadata)

            # Process common drop-ins
            common_dropins = []
            common_dir = self.config.local_config_dir / "systemd" / "common.d"
            if common_dir.exists():
                common_bundle = systemd_bundle / "common.d"
                common_bundle.mkdir(exist_ok=True)
                for dropin in common_dir.glob("*.conf"):
                    dropin_content = dropin.read_text()
                    resolved_dropin = safe_format(dropin_content, **context)
                    (common_bundle / dropin.name).write_text(resolved_dropin)
                    common_dropins.append(dropin.name)

            # Process service-specific drop-ins
            service_dropins = {}
            for service_dropin_dir in (self.config.local_config_dir / "systemd").glob(
                "*.service.d"
            ):
                bundle_dropin_dir = systemd_bundle / service_dropin_dir.name
                bundle_dropin_dir.mkdir()
                dropins = []
                for dropin in service_dropin_dir.glob("*.conf"):
                    dropin_content = dropin.read_text()
                    resolved_dropin = safe_format(dropin_content, **context)
                    (bundle_dropin_dir / dropin.name).write_text(resolved_dropin)
                    dropins.append(dropin.name)
                service_dropins[service_dropin_dir.name] = dropins

            # Resolve and bundle Caddyfile from .fujin/
            if self.config.caddyfile_exists:
                logger.debug("Resolving and bundling Caddyfile")
                caddyfile_content = self.config.caddyfile_path.read_text()
                resolved_caddyfile = safe_format(caddyfile_content, **context)
                (bundle_dir / "Caddyfile").write_text(resolved_caddyfile)

            # Create installer config with unit metadata
            installer_config = {
                "app_name": self.config.app_name,
                "app_dir": self.config.app_dir(self.selected_host),
                "version": version,
                "installation_mode": self.config.installation_mode.value,
                "python_version": self.config.python_version,
                "requirements": bool(self.config.requirements),
                "distfile_name": distfile_path.name,
                "release_command": self.config.release_command,
                "webserver_enabled": self.config.caddyfile_exists,
                "caddy_config_path": self.config.caddy_config_path,
                "app_bin": self.config.app_bin,
                "active_units": self.config.systemd_units,
                "unit_metadata": unit_metadata,
                "common_dropins": common_dropins if discovered_services else [],
                "service_dropins": service_dropins if discovered_services else {},
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

            self._show_deployment_summary(discovered_services, zipapp_path)

            remote_bundle_dir = (
                Path(self.config.app_dir(self.selected_host)) / ".versions"
            )
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
                    self.output.warning(
                        "The uploaded file doesn't match the local file. This could indicate:\n"
                        "  - Network corruption during transfer\n"
                        "  - Storage issues on the remote server\n"
                        "  - Interrupted upload"
                    )

                    if self.no_input or (
                        attempt == max_upload_retries
                        or not Confirm.ask("Upload failed. Retry?")
                    ):
                        self.output.error("Upload verification failed")
                        self.output.info(
                            "\nTroubleshooting:\n"
                            "  - Check your network connection stability\n"
                            "  - Verify the remote server has sufficient disk space: df -h\n"
                            "  - Try deploying again with: fujin deploy"
                        )
                        raise UploadError(
                            "Upload verification failed", checksum_mismatch=True
                        )

                if not upload_ok:
                    self.output.error("Upload failed after maximum retries")
                    raise UploadError("Upload failed after maximum retries")

                self.output.info("Executing remote installation...")
                deploy_script = f"python3 {remote_bundle_path_q} install || (echo 'install failed' >&2; exit 1)"
                if self.config.versions_to_keep:
                    deploy_script += (
                        "&& echo '==> Pruning old versions...' && "
                        f"cd {remote_bundle_dir_q} && "
                        f"ls -1t | tail -n +{self.config.versions_to_keep + 1} | xargs -r rm"
                    )
                conn.run(deploy_script, pty=True)

                # Get git commit hash if available
                git_commit = None
                try:
                    result = subprocess.run(
                        ["git", "rev-parse", "HEAD"],
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                    git_commit = result.stdout.strip()
                except (subprocess.CalledProcessError, FileNotFoundError):
                    pass  # Not a git repo or git not available

                log_operation(
                    connection=conn,
                    app_name=self.config.app_name,
                    operation="deploy",
                    host=self.selected_host.name or self.selected_host.address,
                    version=version,
                    git_commit=git_commit,
                )

        self.output.success("Deployment completed successfully!")

        if self.config.caddyfile_exists:
            domain = self.config.get_domain_name()
            if domain:
                url = f"https://{domain}"
                self.output.info(f"Application is available at: {url}")

    def _show_deployment_summary(
        self, discovered_services: list[ServiceUnit], bundle_path: Path
    ):
        console = Console()

        bundle_size = bundle_path.stat().st_size
        if bundle_size < 1024:
            size_str = f"{bundle_size} B"
        elif bundle_size < 1024 * 1024:
            size_str = f"{bundle_size / 1024:.1f} KB"
        else:
            size_str = f"{bundle_size / (1024 * 1024):.1f} MB"

        # Build summary table
        table = Table(show_header=False, box=None, padding=(0, 1))
        table.add_column("Key", style="bold cyan", width=12)
        table.add_column("Value")

        table.add_row("App", self.config.app_name)
        table.add_row("Version", self.config.version)
        host_display = self.selected_host.name if self.selected_host.name else "default"
        table.add_row("Host", f"{host_display} ({self.selected_host.address})")

        # Build services summary from discovered services
        services_summary = []
        for svc in discovered_services:
            replicas = self.config.replicas.get(svc.name, 1)
            if replicas > 1:
                services_summary.append(f"{svc.name} ({replicas})")
            else:
                services_summary.append(svc.name)
        if services_summary:
            table.add_row("Services", ", ".join(services_summary))
        table.add_row("Bundle", size_str)

        # Display in a panel
        panel = Panel(
            table,
            title="[bold]Deployment Summary[/bold]",
            border_style="blue",
            padding=(1, 1),
            width=60,
        )
        console.print(panel)

        # Confirm unless --no-input is set
        if not self.no_input:
            try:
                if not Confirm.ask(
                    "\n[bold]Proceed with deployment?[/bold]", default=True
                ):
                    raise cappa.Exit("Deployment cancelled", code=0)
            except KeyboardInterrupt:
                raise cappa.Exit("\nDeployment cancelled", code=0)
