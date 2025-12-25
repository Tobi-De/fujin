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
from dataclasses import dataclass
from datetime import datetime
import getpass

import importlib.util
import cappa
from rich.prompt import Confirm
from rich.panel import Panel
from rich.table import Table
from rich.console import Console

from fujin.commands import BaseCommand
from fujin.secrets import resolve_secrets
from fujin.errors import BuildError, UploadError
from fujin.audit import log_operation

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
            new_units, user_units = self.config.render_systemd_units(self.selected_host)
            for name, content in new_units.items():
                (units_dir / name).write_text(content)

            if self.config.webserver.enabled:
                (bundle_dir / "Caddyfile").write_text(self.config.render_caddyfile())

            # Create installer config
            installer_config = {
                "app_name": self.config.app_name,
                "app_dir": self.config.app_dir(self.selected_host),
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

            self._show_deployment_summary(zipapp_path)

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

                self._record_deployment(conn, version)

        log_operation(
            operation="deploy",
            host=self.selected_host.name or self.selected_host.domain_name,
            details={"version": version, "app_name": self.config.app_name},
        )

        self.output.success("Deployment completed successfully!")
        if self.config.webserver.enabled:
            url = f"https://{self.selected_host.domain_name}"
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
        table.add_row("Host", f"{host_display} ({self.selected_host.domain_name})")
        processes_summary = []
        for name, proc in self.config.processes.items():
            if proc.replicas > 1:
                processes_summary.append(f"{name} ({proc.replicas})")
            else:
                processes_summary.append(name)
        table.add_row("Processes", ", ".join(processes_summary))
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

    def _record_deployment(self, conn, version: str):
        git_commit = None
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            )
            git_commit = result.stdout.strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass

        record = {
            "version": version,
            "timestamp": datetime.now().isoformat(),
            "user": getpass.getuser(),
            "host": self.selected_host.name or self.selected_host.domain_name,
            "git_commit": git_commit,
        }

        app_dir = shlex.quote(self.config.app_dir(self.selected_host))
        history_file = f"{app_dir}/.deployments.json"

        stdout, success = conn.run(
            f"cat {history_file} 2>/dev/null || echo '[]'", hide=True, warn=True
        )

        try:
            history = json.loads(stdout) if success and stdout.strip() else []
        except json.JSONDecodeError:
            history = []

        # Add new record (prepend so newest is first), keep only last 50
        history.insert(0, record)
        history = history[:50]

        # Write back to server
        history_json = json.dumps(history)
        conn.run(
            f"cat > {history_file} << 'DEPLOY_HISTORY_EOF'\n{history_json}\nDEPLOY_HISTORY_EOF",
            hide=True,
        )
