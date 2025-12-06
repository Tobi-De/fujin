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
import sys

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
            bundle_dir = Path(tmpdir) / f"{self.config.app_name}-bundle"
            bundle_dir.mkdir()

            # Copy artifacts
            shutil.copy(distfile_path, bundle_dir / distfile_path.name)
            if self.config.requirements:
                shutil.copy(self.config.requirements, bundle_dir / "requirements.txt")

            (bundle_dir / ".env").write_text(parsed_env)

            units_dir = bundle_dir / "units"
            units_dir.mkdir()
            new_units = self.config.render_systemd_units()
            for name, content in new_units.items():
                (units_dir / name).write_text(content)

            if self.config.webserver.enabled:
                (bundle_dir / "Caddyfile").write_text(self.config.render_caddyfile())

            valid_units = set(self.config.active_systemd_units) | set(new_units.keys())
            valid_units_str = " ".join(valid_units)
            install_script = self._generate_install_script(
                version=version,
                distfile_name=distfile_path.name,
                valid_units_str=valid_units_str,
            )

            (bundle_dir / "install.sh").write_text(install_script)
            logger.debug("Generated install script:\n%s", install_script)

            uninstall_script = self._generate_uninstall_script(valid_units_str)
            (bundle_dir / "uninstall.sh").write_text(uninstall_script)
            logger.debug("Generated uninstall script:\n%s", uninstall_script)

            # Create tarball (prefer zstd, fallback to fast gzip)
            tar_created = False
            if sys.version_info.minor >= 14:
                try:
                    logger.info(
                        "Creating zstd-compressed deployment bundle using stdlib"
                    )
                    tar_ext = "tar.zst"
                    tar_path = Path(tmpdir) / f"deploy.{tar_ext}"
                    with tarfile.open(
                        tar_path,
                        "w:zst",
                        format=tarfile.PAX_FORMAT,
                    ) as tar:
                        tar.add(bundle_dir, arcname=".")
                    tar_created = True
                except tarfile.CompressionError:
                    logger.warning(
                        "zstd compression not supported, falling back to gzip"
                    )

            if not tar_created:
                if shutil.which("zstd"):
                    logger.info(
                        "Creating zstd-compressed deployment bundle using system zstd"
                    )
                    tar_ext = "tar.zst"
                    tar_path = Path(tmpdir) / f"deploy.{tar_ext}"
                    subprocess.run(
                        [
                            "tar",
                            "--zstd",
                            "-cf",
                            str(tar_path),
                            "-C",
                            str(bundle_dir),
                            ".",
                        ],
                        check=True,
                    )
                else:
                    logger.info("Creating gzip-compressed deployment bundle")
                    tar_ext = "tar.gz"
                    tar_path = Path(tmpdir) / f"deploy.{tar_ext}"
                    with tarfile.open(
                        tar_path,
                        "w:gz",
                        format=tarfile.PAX_FORMAT,
                    ) as tar:
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
                remote_extract_dir = f"/tmp/{self.config.app_name}-{version}"
                tar_extract_flag = "--zstd" if tar_ext.endswith("zst") else "-z"
                install_cmd = (
                    f"mkdir -p {remote_extract_dir} && "
                    f"tar --overwrite {tar_extract_flag} -xf {remote_bundle_path_q} -C {remote_extract_dir} && "
                    f"cd {remote_extract_dir} && "
                    f"chmod +x install.sh && "
                    f"bash ./install.sh || (echo 'install.sh failed' >&2; exit 1) && "
                    f"cd / && rm -rf {remote_extract_dir}"
                )
                conn.run(install_cmd, pty=True)

        self.stdout.output("[green]Deployment completed successfully![/green]")
        if self.config.webserver.enabled:
            self.stdout.output(
                f"[blue]Application is available at: https://{self.config.host.domain_name}[/blue]"
            )

    def _generate_install_script(
        self, version: str, distfile_name: str, valid_units_str: str
    ) -> str:
        app_dir_q = shlex.quote(self.config.app_dir)
        script = [
            "#!/usr/bin/env bash",
            "set -e",
            "BUNDLE_DIR=$(pwd)",
            'echo "==> Setting up directories..."',
            f"mkdir -p {app_dir_q}",
            f"mv .env {app_dir_q}/.env",
            "echo '==> Installing application...'",
            f"cd {app_dir_q} || exit 1",
            # trap to report failures and keep temp dir if needed
            'trap \'echo "ERROR: install failed at $(date)" >&2; echo "Working dir: $BUNDLE_DIR" >&2; exit 1\' ERR',
        ]

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

        if self.config.release_command:
            script.extend(
                [
                    f"echo '==> Running release command'",
                    f"bash -lc 'cd {app_dir_q} && source .appenv && {self.config.release_command}'",
                ]
            )

        script.extend(
            [
                f"echo '{version}' > .version",
                "cd $BUNDLE_DIR",
                'echo "==> Configuring systemd services..."',
            ]
        )
        script.extend(
            [
                f"sudo cp units/* /etc/systemd/system/",
                systemd_cleanup_script.format(
                    app_name=self.config.app_name, valid_units_str=valid_units_str
                ),
                'echo "==> Restarting services..."',
                f"sudo systemctl enable {' '.join(self.config.active_systemd_units)}",
                f"sudo systemctl restart {' '.join(self.config.active_systemd_units)}",
            ]
        )

        if self.config.webserver.enabled:
            caddy_config_path_q = shlex.quote(self.config.caddy_config_path)
            script.extend(
                [
                    'echo "==> Configuring Caddy..."',
                    "sudo mkdir -p $(dirname {0})".format(caddy_config_path_q),
                    "if caddy validate --config Caddyfile >/dev/null 2>&1; then",
                    f"  sudo mv Caddyfile {caddy_config_path_q}",
                    f"  sudo chown caddy:caddy {caddy_config_path_q}",
                    "  sudo systemctl reload caddy",
                    "else",
                    "  echo 'Caddyfile validation failed, leaving local Caddyfile for inspection' >&2",
                    "fi",
                ]
            )

        if self.config.versions_to_keep:
            script.extend(
                [
                    'echo "==> Pruning old versions..."',
                    f"cd {self.config.app_dir}/.versions",
                    f"ls -1t | tail -n +{self.config.versions_to_keep + 1} | xargs -r rm",
                ]
            )
        script.append("echo '==> Install script completed successfully.'")
        return "\n".join(script)

    def _generate_uninstall_script(self, valid_units_str: str) -> str:
        script = ["#!/usr/bin/env bash", "set -e"]
        app_name = self.config.app_name

        if valid_units_str:
            script.append(f"sudo systemctl disable --now {valid_units_str}")

        script.extend(
            [
                'APP_NAME="' + app_name + '"',
                f"UNITS=({valid_units_str})",
                'for UNIT in "${UNITS[@]}"; do',
                '  case "$UNIT" in',
                '    */*|*..*) echo "Skipping suspicious unit name: $UNIT" >&2; continue;;',
                "  esac",
                '  if [[ "$UNIT" != ${APP_NAME}* ]]; then',
                '    echo "Refusing to remove non-app unit: $UNIT" >&2',
                "    continue",
                "  fi",
                '  sudo rm -f /etc/systemd/system/"$UNIT"',
                "done",
                "sudo systemctl daemon-reload",
                "sudo systemctl reset-failed",
            ]
        )

        if self.config.webserver.enabled:
            script.extend(
                [
                    f"sudo rm -f {shlex.quote(self.config.caddy_config_path)}",
                    "sudo systemctl reload caddy",
                ]
            )
        script.append("echo '==> Uninstall completed.'")
        return "\n".join(script)

    def _get_python_package_install_commands(
        self,
        *,
        distfile_path: str,
        requirements_path: str | None,
    ) -> list[str]:
        logger.info("Generating Python package installation commands")
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
            "echo '==> Syncing Python dependencies...'",
            f"uv python install {self.config.python_version}",
            "test -d .venv || uv venv",
        ]
        if requirements_path:
            commands.append(f"uv pip install -r {requirements_path}")
            commands.append(f"uv pip install --no-deps {distfile_path}")
        else:
            commands.append(f"uv pip install {distfile_path}")
        return commands

    def _get_binary_install_commands(self, distfile_path: str) -> list[str]:
        logger.info("Generating binary installation commands")
        appenv = f"""
set -a  # Automatically export all variables
source .env
set +a  # Stop automatic export
export PATH="{self.config.app_dir}:$PATH"
"""
        full_path_app_bin = f"{self.config.app_dir}/{self.config.app_bin}"
        commands = [
            f"echo '{appenv.strip()}' > {self.config.app_dir}/.appenv",
            "echo '==> Installing binary...'",
            f"cp {distfile_path} {full_path_app_bin}",
            f"chmod +x {full_path_app_bin}",
        ]
        return commands


systemd_cleanup_script = """

APP_NAME="{app_name}"
read -r -a VALID_UNITS <<< "{valid_units_str}"

# Exact match helper
contains_exact() {{
    local needle="$1"; shift
    for x in "$@"; do [[ "$x" == "$needle" ]] && return 0; done
    return 1
}}

echo "==> Discovering installed unit files"
mapfile -t INSTALLED_UNITS < <(
    systemctl list-unit-files --type=service --no-legend --no-pager \\
    | awk -v app="$APP_NAME" '$1 ~ "^"app {{print $1}}'
)

echo "==> Disabling + stopping stale units"
for UNIT in "${{INSTALLED_UNITS[@]}}"; do
    if ! contains_exact "$UNIT" "${{VALID_UNITS[@]}}"; then

        # If it's a template file (myapp@.service), only disable it.
        if [[ "$UNIT" == *@.service ]]; then
            echo "→ Disabling template unit: $UNIT"
            sudo systemctl disable "$UNIT" --quiet || true
        else
            echo "→ Stopping + disabling stale unit: $UNIT"
            sudo systemctl stop "$UNIT" --quiet || true
            sudo systemctl disable "$UNIT" --quiet || true
        fi

        sudo systemctl reset-failed "$UNIT" >/dev/null 2>&1 || true
    fi
done

echo "==> Removing stale service files"
SEARCH_DIRS=(
    /etc/systemd/system/
    /etc/systemd/system/multi-user.target.wants/
)

for DIR in "${{SEARCH_DIRS[@]}}"; do
    [[ -d "$DIR" ]] || continue

    while IFS= read -r -d '' FILE; do
        BASENAME=$(basename "$FILE")
        if ! contains_exact "$BASENAME" "${{VALID_UNITS[@]}}"; then
            echo "→ Removing stale file: $FILE"
            sudo rm -f -- "$FILE"
        fi
    done < <(find "$DIR" -maxdepth 1 -type f -name "${{APP_NAME}}*" -print0)
done

echo "==> Reloading systemd"
sudo systemctl daemon-reload

"""
