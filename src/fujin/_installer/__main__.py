from __future__ import annotations

"""
Zipapp installer - single file with all installation logic.
Run with: python3 installer.pyz [install|uninstall]
"""

import json
import os
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass
class InstallConfig:
    """Configuration for the installer, embedded in the zipapp."""

    app_name: str
    app_dir: str
    version: str
    installation_mode: Literal["python-package", "binary"]
    python_version: str | None
    requirements: bool
    distfile_name: str
    release_command: str | None
    webserver_enabled: bool
    caddy_config_path: str
    app_bin: str
    active_units: list[str]
    unit_metadata: list[dict]  # Pre-computed unit information from deploy phase
    common_dropins: list[str]  # List of common dropin filenames
    service_dropins: (
        dict  # Service-specific dropins: {"web.service.d": ["override.conf"]}
    )


def log(msg: str) -> None:
    print(f"==> {msg}", flush=True)


def run(cmd: str, check: bool = True, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, shell=True, **kwargs)


def install(config: InstallConfig, bundle_dir: Path) -> None:
    """Install the application.

    Assumes it's running from a directory with extracted bundle files.
    """
    log("Setting up directories...")
    app_dir = Path(config.app_dir)
    app_dir.mkdir(parents=True, exist_ok=True)

    # Move .env file
    env_file = bundle_dir / ".env"
    if env_file.exists():
        env_file.rename(app_dir / ".env")

    log("Installing application...")
    os.chdir(app_dir)

    if config.installation_mode == "python-package":
        log("Installing Python package...")
        (app_dir / ".appenv").write_text(f"""set -a
source .env
set +a
export UV_COMPILE_BYTECODE=1
export UV_PYTHON=python{config.python_version}
export PATH=".venv/bin:$PATH"
""")

        log("Syncing Python dependencies...")
        distfile_path = bundle_dir / config.distfile_name
        run(f"uv python install {config.python_version}")
        run("test -d .venv || uv venv")

        if config.requirements:
            requirements_path = bundle_dir / "requirements.txt"
            run(
                f"uv pip install -r {requirements_path} && uv pip install --no-deps {distfile_path}"
            )
        else:
            run(f"uv pip install {distfile_path}")
    else:
        log("Installing binary...")
        (app_dir / ".appenv").write_text(f"""set -a
source .env
set +a
export PATH="{app_dir}:$PATH"
""")
        full_path_app_bin = app_dir / config.app_bin
        full_path_app_bin.unlink(missing_ok=True)
        full_path_app_bin.write_bytes((bundle_dir / config.distfile_name).read_bytes())
        full_path_app_bin.chmod(0o755)

    if config.release_command:
        log("Running release command")
        run(f"bash -lc 'cd {app_dir} && source .appenv && {config.release_command}'")

    (app_dir / ".version").write_text(config.version)
    os.chdir(bundle_dir)

    log("Configuring systemd services...")
    systemd_dir = bundle_dir / "systemd"

    valid_units = []
    for unit in config.unit_metadata:
        valid_units.append(unit["deployed_service"])
        if "deployed_socket" in unit:
            valid_units.append(unit["deployed_socket"])
        if "deployed_timer" in unit:
            valid_units.append(unit["deployed_timer"])

    log("Discovering installed unit files")
    result = run(
        f"systemctl list-unit-files --type=service --no-legend --no-pager | "
        f"awk -v app='{config.app_name}' '$1 ~ \"^\"app {{print $1}}'",
        capture_output=True,
        text=True,
    )
    installed_units = result.stdout.strip().split("\n") if result.stdout.strip() else []

    log("Disabling + stopping stale units")
    for unit in installed_units:
        if unit not in valid_units:
            if unit.endswith("@.service"):
                print(f"→ Disabling template unit: {unit}")
                run(f"sudo systemctl disable {unit} --quiet", check=False)
            else:
                print(f"→ Stopping + disabling stale unit: {unit}")
                run(
                    f"sudo systemctl stop {unit} --quiet && sudo systemctl disable {unit} --quiet",
                    check=False,
                )
            run(f"sudo systemctl reset-failed {unit}", check=False, capture_output=True)

    log("Removing stale service files")
    for search_dir in [
        "/etc/systemd/system/",
        "/etc/systemd/system/multi-user.target.wants/",
    ]:
        if not Path(search_dir).exists():
            continue
        for file_path in Path(search_dir).glob(f"{config.app_name}*"):
            if file_path.is_file() and file_path.name not in valid_units:
                print(f"→ Removing stale file: {file_path}")
                run(f"sudo rm -f {file_path}")

    log("Installing new service files...")
    for unit in config.unit_metadata:
        # Copy main service file
        service_file = systemd_dir / unit["service_file"]
        content = service_file.read_text()
        deployed_path = Path("/etc/systemd/system") / unit["deployed_service"]
        run(
            f"sudo tee {deployed_path} > /dev/null",
            input=content,
            text=True,
            check=True,
        )

        # Copy socket file if exists
        if "socket_file" in unit:
            socket_file = systemd_dir / unit["socket_file"]
            socket_content = socket_file.read_text()
            socket_deployed_path = Path("/etc/systemd/system") / unit["deployed_socket"]
            run(
                f"sudo tee {socket_deployed_path} > /dev/null",
                input=socket_content,
                text=True,
                check=True,
            )

        # Copy timer file if exists
        if "timer_file" in unit:
            timer_file = systemd_dir / unit["timer_file"]
            timer_content = timer_file.read_text()
            timer_deployed_path = Path("/etc/systemd/system") / unit["deployed_timer"]
            run(
                f"sudo tee {timer_deployed_path} > /dev/null",
                input=timer_content,
                text=True,
                check=True,
            )

    # Deploy common dropins (apply to all services)
    if config.common_dropins:
        common_dir = systemd_dir / "common.d"
        for dropin_name in config.common_dropins:
            dropin_content = (common_dir / dropin_name).read_text()

            # Apply to all services from metadata
            for unit in config.unit_metadata:
                deployed_service = unit["deployed_service"]
                dropin_dir = Path("/etc/systemd/system") / f"{deployed_service}.d"
                run(f"sudo mkdir -p {dropin_dir}")
                dropin_dest = dropin_dir / dropin_name
                run(
                    f"sudo tee {dropin_dest} > /dev/null",
                    input=dropin_content,
                    text=True,
                    check=True,
                )

    # Deploy service-specific dropins
    for service_dropin_dirname, dropin_names in config.service_dropins.items():
        service_dropin_dir = systemd_dir / service_dropin_dirname
        service_file_name = service_dropin_dirname.removesuffix(".d")

        # Find matching unit from metadata
        matching_unit = None
        for unit in config.unit_metadata:
            if unit["service_file"] == service_file_name:
                matching_unit = unit
                break

        if matching_unit:
            deployed_dropin_dir = (
                Path("/etc/systemd/system") / f"{matching_unit['deployed_service']}.d"
            )
            run(f"sudo mkdir -p {deployed_dropin_dir}")

            for dropin_name in dropin_names:
                dropin_content = (service_dropin_dir / dropin_name).read_text()
                dropin_dest = deployed_dropin_dir / dropin_name
                run(
                    f"sudo tee {dropin_dest} > /dev/null",
                    input=dropin_content,
                    text=True,
                    check=True,
                )

    log("Restarting services...")
    units_str = " ".join(config.active_units)
    run(
        f"sudo systemctl daemon-reload && sudo systemctl enable {units_str} && sudo systemctl restart {units_str}"
    )

    if config.webserver_enabled:
        log("Configuring Caddy...")
        caddy_config_dir = Path(config.caddy_config_path).parent
        run(f"sudo mkdir -p {caddy_config_dir}")

        caddyfile_path = bundle_dir / "Caddyfile"
        if caddyfile_path.exists():
            if (
                run(
                    f"caddy validate --config {caddyfile_path}",
                    check=False,
                    capture_output=True,
                ).returncode
                == 0
            ):
                run(
                    f"sudo cp {caddyfile_path} {config.caddy_config_path} && "
                    f"sudo chown caddy:caddy {config.caddy_config_path} && "
                    f"sudo systemctl reload caddy"
                )
            else:
                print(
                    "Caddyfile validation failed, leaving local Caddyfile for inspection",
                    file=sys.stderr,
                )

    log("Install completed successfully.")


def uninstall(config: InstallConfig, bundle_dir: Path) -> None:
    """Uninstall the application.

    Assumes it's running from a directory with extracted bundle files.
    """
    log("Uninstalling application...")
    log("Stopping and disabling services...")

    # Build list of units from metadata
    valid_units = []
    for unit in config.unit_metadata:
        valid_units.append(unit["deployed_service"])
        if "deployed_socket" in unit:
            valid_units.append(unit["deployed_socket"])
        if "deployed_timer" in unit:
            valid_units.append(unit["deployed_timer"])

    regular_units = [
        u
        for u in valid_units
        if not u.endswith("@.service")
        and not u.endswith("@.socket")
        and not u.endswith("@.timer")
    ]
    template_units = [
        u
        for u in valid_units
        if u.endswith("@.service") or u.endswith("@.socket") or u.endswith("@.timer")
    ]

    if regular_units:
        run(
            f"sudo systemctl disable --now {' '.join(regular_units)} --quiet",
            check=False,
        )
    if template_units:
        run(f"sudo systemctl disable {' '.join(template_units)} --quiet", check=False)

    log("Removing systemd unit files...")
    for unit in valid_units:
        if not unit.startswith(config.app_name):
            print(f"Refusing to remove non-app unit: {unit}", file=sys.stderr)
            continue
        run(f"sudo rm -f /etc/systemd/system/{unit}")

    run("sudo systemctl daemon-reload && sudo systemctl reset-failed", check=False)

    if config.webserver_enabled:
        log("Removing Caddy configuration...")
        run(f"sudo rm -f {config.caddy_config_path} && sudo systemctl reload caddy")

    log("Uninstall completed.")


def main() -> None:
    """Main entry point.

    Handles extraction of zipapp to temp directory and cleanup.
    """
    if len(sys.argv) < 2:
        print("Usage: python3 installer.pyz [install|uninstall]", file=sys.stderr)
        sys.exit(1)

    command = sys.argv[1]

    if command not in ("install", "uninstall"):
        print(f"Unknown command: {command}", file=sys.stderr)
        print("Usage: python3 installer.pyz [install|uninstall]", file=sys.stderr)
        sys.exit(1)

    source_path = Path(__file__).parent
    zipapp_file = str(source_path)

    with tempfile.TemporaryDirectory(
        prefix=f"fujin-{command}-{source_path.name}"
    ) as tmpdir:
        try:
            log("Extracting installer bundle...")
            with zipfile.ZipFile(zipapp_file, "r") as zf:
                zf.extractall(tmpdir)

            # Change to temp directory and run command
            original_dir = os.getcwd()
            os.chdir(tmpdir)

            bundle_dir = Path(tmpdir)
            config_path = bundle_dir / "config.json"
            config = InstallConfig(**json.loads(config_path.read_text()))
            try:
                if command == "install":
                    install(config, bundle_dir)
                else:
                    uninstall(config, bundle_dir)
            finally:
                os.chdir(original_dir)

        except Exception as e:
            print(f"ERROR: {command} failed: {e}", file=sys.stderr)
            import traceback

            traceback.print_exc()
            sys.exit(1)


if __name__ == "__main__":
    main()
