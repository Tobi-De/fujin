from __future__ import annotations
import hashlib
import importlib.util
import json
import logging
import shlex
import shutil
import subprocess
import tempfile
import zipapp
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

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

        if not self.config.deployed_units:
            raise DeploymentError("No systemd units found, nothing to deploy")

        with tempfile.TemporaryDirectory() as tmpdir:
            self.output.info("Preparing deployment bundle...")
            bundle_dir = Path(tmpdir) / f"{self.config.app_name}-bundle"
            bundle_dir.mkdir()

            context = {
                "app_name": self.config.app_name,
                "app_user": self.config.app_user,
                "version": version,
                "app_dir": self.config.app_dir,
                "install_dir": self.config.install_dir,
                "user": self.selected_host.user,
            }

            # Copy artifacts
            shutil.copy(distfile_path, bundle_dir / distfile_path.name)
            if self.config.requirements:
                shutil.copy(self.config.requirements, bundle_dir / "requirements.txt")

            # Track unresolved variables across all files
            all_unresolved = set()

            # resolve and copy env file
            resolved_env, unresolved = safe_format(parsed_env, **context)
            all_unresolved.update(unresolved)
            (bundle_dir / ".env").write_text(resolved_env)

            logger.debug("Validating and resolving systemd units")
            systemd_bundle = bundle_dir / "systemd"
            systemd_bundle.mkdir()

            for du in self.config.deployed_units:
                # Validate and resolve main service file
                service_content = du.service_file.read_text()
                resolved_content, unresolved = safe_format(service_content, **context)
                all_unresolved.update(unresolved)

                (systemd_bundle / du.service_file.name).write_text(resolved_content)

                # Process and add socket file if exists
                if du.socket_file:
                    socket_content = du.socket_file.read_text()
                    resolved_socket, unresolved = safe_format(socket_content, **context)
                    all_unresolved.update(unresolved)
                    (systemd_bundle / du.socket_file.name).write_text(resolved_socket)

                # Process and add timer file if exists
                if du.timer_file:
                    timer_content = du.timer_file.read_text()
                    resolved_timer, unresolved = safe_format(timer_content, **context)
                    all_unresolved.update(unresolved)
                    (systemd_bundle / du.timer_file.name).write_text(resolved_timer)

            # Build installer metadata from deployed units
            deployed_units_data = []
            for du in self.config.deployed_units:
                unit_dict = {
                    "service_name": du.service_name,
                    "is_template": du.is_template,
                    "service_file": du.service_file.name,
                    "socket_file": du.socket_file.name if du.socket_file else None,
                    "timer_file": du.timer_file.name if du.timer_file else None,
                    "template_service_name": du.template_service_name,
                    "template_socket_name": du.template_socket_name,
                    "template_timer_name": du.template_timer_name,
                    "replica_count": du.replica_count,
                    "instance_service_names": du.instance_service_names,
                }
                deployed_units_data.append(unit_dict)

            # Handle common dropins
            common_dir = self.config.local_config_dir / "systemd" / "common.d"
            if common_dir.exists():
                common_bundle = systemd_bundle / "common.d"
                common_bundle.mkdir(exist_ok=True)
                for dropin in common_dir.glob("*.conf"):
                    dropin_content = dropin.read_text()
                    resolved_dropin, unresolved = safe_format(dropin_content, **context)
                    all_unresolved.update(unresolved)
                    (common_bundle / dropin.name).write_text(resolved_dropin)

            # Handle service-specific dropins
            for service_dropin_dir in (self.config.local_config_dir / "systemd").glob(
                "*.service.d"
            ):
                bundle_dropin_dir = systemd_bundle / service_dropin_dir.name
                bundle_dropin_dir.mkdir()
                for dropin in service_dropin_dir.glob("*.conf"):
                    dropin_content = dropin.read_text()
                    resolved_dropin, unresolved = safe_format(dropin_content, **context)
                    all_unresolved.update(unresolved)
                    (bundle_dropin_dir / dropin.name).write_text(resolved_dropin)

            if self.config.caddyfile_exists:
                logger.debug("Resolving and bundling Caddyfile")
                caddyfile_content = self.config.caddyfile_path.read_text()
                resolved_caddyfile, unresolved = safe_format(
                    caddyfile_content, **context
                )
                all_unresolved.update(unresolved)
                (bundle_dir / "Caddyfile").write_text(resolved_caddyfile)

            # Warn about unresolved variables
            if all_unresolved:
                self.output.warning(
                    f"Found unresolved variables in configuration files: {', '.join(sorted(all_unresolved))}\n"
                    f"Available variables: {', '.join(sorted(context.keys()))}\n"
                    "These will be left as-is (e.g., {variable_name}) in deployed files."
                )

            installer_config = {
                "app_name": self.config.app_name,
                "app_user": self.config.app_user,
                "deploy_user": self.selected_host.user,
                "app_dir": self.config.app_dir,
                "version": version,
                "installation_mode": self.config.installation_mode.value,
                "python_version": self.config.python_version,
                "requirements": bool(self.config.requirements),
                "distfile_name": distfile_path.name,
                "webserver_enabled": self.config.caddyfile_exists,
                "caddy_config_path": self.config.caddy_config_path,
                "app_bin": self.config.app_name,  # Just the binary name, not full path
                "deployed_units": deployed_units_data,
            }

            # Create zipapp
            logger.info("Creating Python zipapp installer")
            zipapp_dir = Path(tmpdir) / "zipapp_source"
            zipapp_dir.mkdir()

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

            self._show_deployment_summary(zipapp_path)

            remote_bundle_dir = Path(self.config.install_dir) / ".versions"
            remote_bundle_path = (
                f"{remote_bundle_dir}/{self.config.app_name}-{version}.pyz"
            )

            # Quote remote paths for shell usage (safe insertion into remote commands)
            remote_bundle_dir_q = shlex.quote(str(remote_bundle_dir))
            remote_bundle_path_q = shlex.quote(str(remote_bundle_path))

            # Upload and Execute
            with self.connection() as conn:
                conn.run(f"mkdir -p {remote_bundle_dir_q}")

                self.output.info("Uploading deployment bundle...")
                conn.put(str(zipapp_path), remote_bundle_path_q)

                logger.info("Verifying uploaded bundle checksum")
                remote_checksum_out, _ = conn.run(
                    f"sha256sum {remote_bundle_path_q} | awk '{{print $1}}'",
                    hide=True,
                )
                remote_checksum = remote_checksum_out.strip()

                if local_checksum != remote_checksum:
                    self.output.error(
                        f"Upload verification failed! Local: {local_checksum}, Remote: {remote_checksum}"
                    )
                    raise UploadError(
                        "Upload verification failed", checksum_mismatch=True
                    )

                self.output.success("Bundle uploaded and verified successfully.")

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

    def _show_deployment_summary(self, bundle_path: Path):
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

        # Build services summary from deployed units
        services_summary = []
        for du in self.config.deployed_units:
            if du.replica_count > 1:
                services_summary.append(f"{du.service_name} ({du.replica_count})")
            else:
                services_summary.append(du.service_name)
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
