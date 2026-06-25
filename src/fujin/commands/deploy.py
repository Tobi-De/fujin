from __future__ import annotations

import json
import logging
import shlex
import shutil
import hashlib
import subprocess
import tempfile
import zipapp
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Generator

import cappa
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm
from rich.table import Table

import fujin._installer as installer
from fujin.audit import log_operation
from fujin.commands import BaseCommand
from fujin.commands.rollback import Rollback
from fujin.config import get_git_short_hash
from fujin import connection
from fujin.connection import SSH2Connection
from fujin.errors import (
    BuildError,
    CommandError,
    DeploymentError,
    UploadError,
)
from fujin.formatting import safe_format
from fujin.secrets import resolve_secrets

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
    full_restart: Annotated[
        bool,
        cappa.Arg(
            long="--full-restart",
            help="Force a full restart instead of reload-or-restart",
        ),
    ] = False
    restart_on_env_change: Annotated[
        bool,
        cappa.Arg(
            long="--restart-on-env-change",
            help="Force full restart if environment variables changed",
        ),
    ] = False
    no_rollback: Annotated[
        bool,
        cappa.Arg(
            long="--no-rollback",
            help="Disable automatic rollback on deployment failure",
        ),
    ] = False
    bundle_dir: Annotated[
        Path | None,
        cappa.Arg(
            long="--bundle-dir",
            help="Directory to create bundle in (preserved after deploy, fails if exists)",
        ),
    ] = None

    def __call__(self):
        logger.debug("Starting deployment for %s", self.config.app_name)
        logger.debug("Target host: %s", self.selected_host.address)

        if self.config.secret_config:
            self.output.info("Resolving secrets from configuration...")
            logger.debug("Using secret adapter: %s", self.config.secret_config.adapter)
            parsed_env = resolve_secrets(
                self.selected_host.env_content, self.config.secret_config
            )
        else:
            parsed_env = self.selected_host.env_content

        try:
            logger.debug("Build command: %s", self.config.build_command)
            self.output.info(f"Building application ...")
            subprocess.run(
                self.config.build_command,
                check=True,
                shell=True,
                timeout=self.config.build_timeout,
            )
        except subprocess.TimeoutExpired:
            self.output.error(
                f"Build command timed out after {self.config.build_timeout}s"
            )
            self.output.info(
                f"Command: {self.config.build_command}\n\n"
                "Troubleshooting:\n"
                "  - The build is taking too long; increase build_timeout in fujin.toml\n"
                "  - Check for infinite loops or hung subprocesses in your build\n"
                "  - Try running the build command manually to see its progress"
            )
            raise BuildError(
                "Build timed out", command=self.config.build_command
            ) from None
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
        if self.config.requirements:
            req_file = Path(self.config.requirements)
            if req_file.exists():
                with open(req_file, "rb") as f:
                    logger.debug(
                        "Requirements file hash: %s",
                        hashlib.file_digest(f, "sha256").hexdigest(),
                    )
            else:
                self.output.error(
                    f"Requirements file not found: {self.config.requirements}"
                )
                self.output.info(
                    "\nTroubleshooting:\n"
                    "  - Ensure your build_command generates the requirements file\n"
                    "  - Check that the 'requirements' path in fujin.toml is correct\n"
                    f"  - Try running: uv pip compile pyproject.toml -o {self.config.requirements}"
                )
                raise BuildError(
                    f"Requirements file not found: {self.config.requirements}"
                )

        version = self.config.version
        git_commit = get_git_short_hash()
        bundle_version = self.config.local_version
        distfile_path = self.config.get_distfile_path(version)
        logger.debug("Version: %s (bundle: %s)", version, bundle_version)
        logger.debug("Distfile: %s", distfile_path)
        if git_commit:
            logger.debug("Git commit: %s", git_commit)

        if not self.config.deployed_units:
            raise DeploymentError("No systemd units found, nothing to deploy")

        with self._bundle_directory() as tmpdir:
            self.output.info("Preparing deployment bundle...")
            zipapp_dir = Path(tmpdir) / "zipapp_source"
            zipapp_dir.mkdir()

            context = {
                "app_name": self.config.app_name,
                "app_user": self.config.app_user,
                "version": version,
                "app_dir": self.config.app_dir,
                "shared_dir": self.config.shared_dir,
                "user": self.selected_host.user,
            }

            # Add unit names to context for cross-service references (e.g., After={web_socket})
            # Only expose singleton units that make sense as dependency targets:
            # - socket/timer if they exist (always singletons)
            # - service only if no socket/timer and single replica
            for du in self.config.deployed_units:
                if du.socket_file:
                    context[f"{du.name}_socket"] = du.template_socket_name
                if du.timer_file:
                    context[f"{du.name}_timer"] = du.template_timer_name
                if not du.socket_file and not du.timer_file and not du.is_template:
                    context[f"{du.name}"] = du.template_service_name

            # Copy installer entry point
            shutil.copy(installer.__file__, zipapp_dir / "__main__.py")

            logger.debug("Copying distfile to bundle")
            shutil.copy(distfile_path, zipapp_dir / distfile_path.name)
            if self.config.requirements:
                logger.debug("Copying requirements.txt to bundle")
                shutil.copy(self.config.requirements, zipapp_dir / "requirements.txt")

            # Track unresolved variables across all files
            all_unresolved = set()

            # Resolve env file (uploaded separately, not bundled)
            resolved_env, unresolved = safe_format(parsed_env, **context)
            all_unresolved.update(unresolved)

            logger.debug("Validating and resolving systemd units")
            systemd_dir = zipapp_dir / "systemd"
            systemd_dir.mkdir()

            logger.debug(
                "Processing %d deployed units", len(self.config.deployed_units)
            )
            for du in self.config.deployed_units:
                # Validate and resolve main service file
                logger.debug("Processing unit: %s", du.name)
                service_content = du.service_file.read_text()
                resolved_content, unresolved = safe_format(service_content, **context)
                all_unresolved.update(unresolved)

                (systemd_dir / du.service_file.name).write_text(resolved_content)

                # Process and add socket file if exists
                if du.socket_file:
                    logger.debug("  Including socket file: %s", du.socket_file.name)
                    socket_content = du.socket_file.read_text()
                    resolved_socket, unresolved = safe_format(socket_content, **context)
                    all_unresolved.update(unresolved)
                    (systemd_dir / du.socket_file.name).write_text(resolved_socket)

                # Process and add timer file if exists
                if du.timer_file:
                    logger.debug("  Including timer file: %s", du.timer_file.name)
                    timer_content = du.timer_file.read_text()
                    resolved_timer, unresolved = safe_format(timer_content, **context)
                    all_unresolved.update(unresolved)
                    (systemd_dir / du.timer_file.name).write_text(resolved_timer)

            # Build installer metadata from deployed units
            deployed_units_data = []
            for du in self.config.deployed_units:
                unit_dict = {
                    "name": du.name,
                    "service_file": du.service_file.name,
                    "socket_file": du.socket_file.name if du.socket_file else None,
                    "timer_file": du.timer_file.name if du.timer_file else None,
                    "replicas": du.replicas,
                    "is_template": du.is_template,
                    "service_instances": du.service_instances(),
                    "template_service_name": du.template_service_name,
                    "template_socket_name": du.template_socket_name,
                    "template_timer_name": du.template_timer_name,
                }
                deployed_units_data.append(unit_dict)

            # Handle common dropins
            common_dir = self.config.local_config_dir / "systemd" / "common.d"
            if common_dir.exists():
                common_bundle = systemd_dir / "common.d"
                common_bundle.mkdir(exist_ok=True)
                common_dropins = list(common_dir.glob("*.conf"))
                if common_dropins:
                    logger.debug("Processing %d common dropins", len(common_dropins))
                for dropin in common_dropins:
                    dropin_content = dropin.read_text()
                    resolved_dropin, unresolved = safe_format(dropin_content, **context)
                    all_unresolved.update(unresolved)
                    (common_bundle / dropin.name).write_text(resolved_dropin)
                    logger.debug("  Bundled common dropin: %s", dropin.name)

            # Handle service-specific dropins
            for service_dropin_dir in (self.config.local_config_dir / "systemd").glob(
                "*.service.d"
            ):
                bundle_dropin_dir = systemd_dir / service_dropin_dir.name
                bundle_dropin_dir.mkdir()
                dropins = list(service_dropin_dir.glob("*.conf"))
                logger.debug(
                    "Processing %d dropins for %s",
                    len(dropins),
                    service_dropin_dir.name,
                )
                for dropin in dropins:
                    dropin_content = dropin.read_text()
                    resolved_dropin, unresolved = safe_format(dropin_content, **context)
                    all_unresolved.update(unresolved)
                    (bundle_dropin_dir / dropin.name).write_text(resolved_dropin)
                    logger.debug("  Bundled dropin: %s", dropin.name)

            if self.config.caddyfile_exists:
                logger.debug("Resolving and bundling Caddyfile")
                caddyfile_content = self.config.caddyfile_path.read_text()
                resolved_caddyfile, unresolved = safe_format(
                    caddyfile_content, **context
                )
                all_unresolved.update(unresolved)
                (zipapp_dir / "Caddyfile").write_text(resolved_caddyfile)

            # Resolve hook commands
            resolved_hooks: dict[str, list[str]] = {}
            if self.config.hooks_config:
                for phase in (
                    "pre_install",
                    "post_install",
                    "post_start",
                ):
                    commands = getattr(self.config.hooks_config, phase)
                    resolved_commands = []
                    for cmd in commands:
                        resolved, unresolved = safe_format(cmd, **context)
                        all_unresolved.update(unresolved)
                        resolved_commands.append(resolved)
                    if resolved_commands:
                        resolved_hooks[phase] = resolved_commands

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
                "shared_dir": self.config.shared_dir,
                "version": bundle_version,
                "installation_mode": self.config.installation_mode.value,
                "python_version": self.config.python_version,
                "requirements": bool(self.config.requirements),
                "distfile_name": distfile_path.name,
                "webserver_enabled": self.config.caddyfile_exists,
                "caddy_config_path": self.config.caddy_config_path,
                "app_bin": self.config.app_name,
                "deployed_units": deployed_units_data,
                "hooks": resolved_hooks,
                "versions_to_keep": self.config.versions_to_keep or 3,
            }

            # Write config without indent for smaller size
            (zipapp_dir / "config.json").write_text(json.dumps(installer_config))

            logger.debug("Creating Python zipapp installer")
            zipapp_path = Path(tmpdir) / "installer.pyz"
            zipapp.create_archive(
                zipapp_dir,
                zipapp_path,
                interpreter="/usr/bin/env python3",
            )
            logger.debug("Created zipapp at %s", zipapp_path)

            bundle_size = zipapp_path.stat().st_size
            self._show_deployment_summary(bundle_size, bundle_version)

            remote_bundle_path = (
                f"/tmp/fujin-{self.config.app_name}-{bundle_version}.pyz"
            )
            remote_bundle_path_q = shlex.quote(remote_bundle_path)

            # Minimum size threshold for rsync (30MB) — below this, overhead isn't worth it
            min_rsync_size = 30 * 1024 * 1024

            # Upload and Execute
            with self._deploy_session() as conn:
                # Check rsync availability (single round trip)
                use_rsync = False
                if bundle_size >= min_rsync_size:
                    output, _ = conn.run("command -v rsync", warn=True, hide=True)
                    use_rsync = bool(output.strip())

                self.output.info("Uploading deployment bundle...")
                if use_rsync:
                    staging_path = "/tmp/.fujin-staging.pyz"
                    staging_path_q = shlex.quote(staging_path)
                    logger.debug("Using rsync for delta upload")
                    try:
                        conn.rsync_upload(str(zipapp_path), staging_path_q)
                        conn.run(
                            f"cp -f {staging_path_q} {remote_bundle_path_q}",
                            hide=True,
                        )
                    except (FileNotFoundError, UploadError) as e:
                        self.output.warning(f"rsync failed ({e}), falling back to SCP")
                        use_rsync = False

                if not use_rsync:
                    logger.debug("Using SCP for upload")
                    conn.put(str(zipapp_path), remote_bundle_path_q, verify=True)

                self.output.success("Bundle uploaded successfully.")

                self.output.info("Executing remote installation...")

                # Write .env file to shared/ (persistent across releases)
                remote_env_path = f"{self.config.shared_dir}/.env"
                remote_env_path_q = shlex.quote(remote_env_path)
                shared_dir_q = shlex.quote(self.config.shared_dir)
                remote_env_backup_q = shlex.quote(f"{remote_env_path}.bak")
                logger.debug("Writing .env to %s", remote_env_path)

                # Backup existing .env (ensure shared dir exists first)
                conn.run(
                    f"mkdir -p {shared_dir_q} && "
                    f"cp {remote_env_path_q} {remote_env_backup_q} 2>/dev/null || true",
                    hide=True,
                )

                if self.restart_on_env_change:
                    new_env_hash = hashlib.sha256(resolved_env.encode()).hexdigest()
                    old_hash_output, _ = conn.run(
                        f"sha256sum {remote_env_path_q} 2>/dev/null | cut -d' ' -f1 || true",
                        hide=True,
                    )
                    old_env_hash = old_hash_output.strip()
                    if old_env_hash and old_env_hash != new_env_hash:
                        self.output.info(
                            "Environment variables changed, forcing full restart..."
                        )
                        self.full_restart = True

                # Upload .env via SCP (avoids shell escaping issues with base64 piping)
                with tempfile.NamedTemporaryFile(mode="w", suffix=".env") as tmp_env:
                    tmp_env.write(resolved_env)
                    tmp_env.flush()
                    conn.put(tmp_env.name, remote_env_path_q)

                # chown may fail on first deploy if app_user doesn't exist yet
                # (installer creates it), so use || true to make it non-fatal
                conn.run(
                    f"chmod 640 {remote_env_path_q} && "
                    f"(chown {self.selected_host.user}:{self.config.app_user} {remote_env_path_q} || true)",
                    hide=True,
                )

                # Run pre-install hooks (as deploy user, with .env sourced)
                pre_install_commands = resolved_hooks.get("pre_install", [])
                if pre_install_commands:
                    self.output.info("Running pre-install hooks...")
                    try:
                        for cmd in pre_install_commands:
                            self.output.info(f"  [pre-install] {cmd}")
                            full_cmd = (
                                f"set -a; source {shared_dir_q}/.env 2>/dev/null; set +a; "
                                f"{cmd}"
                            )
                            conn.run(full_cmd, pty=True)
                    except CommandError:
                        conn.run(
                            f"mv {remote_env_backup_q} {remote_env_path_q} 2>/dev/null || true",
                            hide=True,
                        )
                        raise

                rollback_ran = False
                rollback_succeeded = False
                try:
                    install_cmd = f"sudo python3 {remote_bundle_path_q} install"
                    if self.full_restart:
                        install_cmd += " --full-restart"
                    if self.verbose > 0:
                        install_cmd += f" --verbose {self.verbose}"
                    conn.run(install_cmd, pty=True)
                except CommandError as e:
                    if e.code != installer.EXIT_SERVICE_START_FAILED:
                        conn.run(f"rm -f {remote_env_backup_q}", warn=True, hide=True)
                        raise DeploymentError(
                            f"Installation failed with exit code {e.code}"
                        ) from e

                    if self.no_rollback:
                        conn.run(f"rm -f {remote_env_backup_q}", warn=True, hide=True)
                        raise DeploymentError(
                            "Services failed to start. Rollback disabled via --no-rollback."
                        ) from e

                    # Restore previous .env before rollback
                    self.output.info("Restoring previous environment...")
                    conn.run(
                        f"mv {remote_env_backup_q} {remote_env_path_q} 2>/dev/null || true",
                        hide=True,
                    )

                    rollback = Rollback(host=self.host, previous=True, strict=True)
                    self.output.info(
                        "Services failed to start. Rolling back to previous version."
                    )
                    try:
                        if self.no_input or Confirm.ask(
                            "\n[bold yellow]Proceed with rollback?[/bold yellow]",
                            default=True,
                        ):
                            rollback_result = rollback()
                            rollback_ran = True
                            rollback_succeeded = rollback_result == 1
                        else:
                            raise DeploymentError(
                                f"Installation failed with exit code {e.code}"
                            ) from e
                    except KeyboardInterrupt:
                        self.output.info("\nRollback cancelled.")
                    finally:
                        # Always remove the failed bundle to prevent it from
                        # appearing in future rollback options
                        self.output.info("Removing failed deployment bundle...")
                        conn.run(f"rm -f {remote_bundle_path_q}", warn=True)

                if not rollback_ran:
                    conn.run(f"rm -f {remote_bundle_path_q}", warn=True, hide=True)
                    conn.run(f"rm -f {remote_env_backup_q}", warn=True, hide=True)

                # Get git commit hash if available
                log_operation(
                    connection=conn,
                    app_name=self.config.app_name,
                    operation="deploy",
                    host=self.selected_host.name or self.selected_host.address,
                    version=bundle_version,
                    git_commit=git_commit,
                )

        if not rollback_ran:
            self.output.success("Deployment completed successfully!")

        if self.config.caddyfile_exists and (not rollback_ran or rollback_succeeded):
            domain = self.config.get_domain_name()
            if domain:
                url = f"https://{domain}"
                self.output.info(f"Application is available at: {url}")

    @contextmanager
    def _bundle_directory(self) -> Generator[Path, None, None]:
        """Context manager for bundle directory.

        If bundle_dir is specified, uses that directory (fails if exists).
        Otherwise, creates a temporary directory that is cleaned up on exit.
        """
        if self.bundle_dir:
            if self.bundle_dir.exists():
                raise DeploymentError(
                    f"Bundle directory already exists: {self.bundle_dir}"
                )
            self.bundle_dir.mkdir(parents=True)
            logger.debug("Using bundle directory: %s", self.bundle_dir)
            yield self.bundle_dir
            self.output.info(f"Bundle preserved in: {self.bundle_dir}")
        else:
            with tempfile.TemporaryDirectory() as tmpdir:
                yield Path(tmpdir)

    @contextmanager
    def _deploy_session(self) -> Generator[SSH2Connection, None, None]:
        """Context manager that combines SSH connection + deployment lock.

        Acquires the deploy lock on the remote server before yielding the
        connection, and releases it on exit (even on error).
        """
        with connection.connection(host=self.selected_host, compress=False) as conn:
            app_dir_q = shlex.quote(self.config.app_dir)
            lock_file_q = shlex.quote(f"{self.config.app_dir}/.deploy_lock")
            _, acquired = conn.run(
                f"mkdir -p {app_dir_q} && (set -C; echo $$ > {lock_file_q}) 2>/dev/null",
                warn=True,
                hide=True,
            )
            if not acquired:
                raise DeploymentError(
                    "Another deployment is in progress.\n"
                    "If you are sure no deploy is running, remove the lock manually:\n"
                    f"  ssh {self.selected_host.user}@{self.selected_host.address} rm {lock_file_q}"
                )

            try:
                yield conn
            finally:
                conn.run(f"rm -f {lock_file_q}", warn=True, hide=True)

    def _show_deployment_summary(self, bundle_size: int, bundle_version: str):
        console = Console()

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
        table.add_row("Version", bundle_version)
        host_display = self.selected_host.name if self.selected_host.name else "default"
        table.add_row("Host", f"{host_display} ({self.selected_host.address})")

        # Build services summary from deployed units
        services_summary = []
        for du in self.config.deployed_units:
            if du.replicas > 1:
                services_summary.append(f"{du.name} ({du.replicas})")
            else:
                services_summary.append(du.name)
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
