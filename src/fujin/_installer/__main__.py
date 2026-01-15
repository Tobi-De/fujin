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
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypedDict


class DeployedUnitDict(TypedDict):
    """Type hint for deployed unit serialized as dict."""

    service_name: str
    is_template: bool
    service_file: str  # filename only
    socket_file: str | None
    timer_file: str | None
    template_service_name: str
    template_socket_name: str | None
    template_timer_name: str | None
    replica_count: int
    instance_service_names: list[str]


@dataclass
class InstallConfig:
    """Configuration for the installer, embedded in the zipapp."""

    app_name: str
    app_user: str
    deploy_user: str
    app_dir: str
    version: str
    installation_mode: Literal["python-package", "binary"]
    python_version: str | None
    requirements: bool
    distfile_name: str
    webserver_enabled: bool
    caddy_config_path: str
    app_bin: str
    deployed_units: list[DeployedUnitDict]


# Constants
SYSTEMD_SYSTEM_DIR = Path("/etc/systemd/system")
SYSTEMD_WANTS_DIR = SYSTEMD_SYSTEM_DIR / "multi-user.target.wants"


def log(msg: str) -> None:
    print(f"==> {msg}", flush=True)


def run(cmd: str, check: bool = True, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, shell=True, **kwargs)


def install(config: InstallConfig, bundle_dir: Path) -> None:
    """Install the application.
    Assumes it's running from a directory with extracted bundle files.
    """

    log("Creating app user if needed...")
    user_exists = (
        run(f"id -u {config.app_user}", check=False, capture_output=True).returncode
        == 0
    )
    if not user_exists:
        log(f"Creating system user: {config.app_user}")
        run(
            f"sudo useradd --system --no-create-home --shell /usr/sbin/nologin {config.app_user}"
        )

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

        uv_python_install_dir = "UV_PYTHON_INSTALL_DIR=/opt/fujin/.python"

        (app_dir / ".appenv").write_text(f"""set -a
source .env
set +a
export {uv_python_install_dir}
export PATH=".venv/bin:$PATH"

# Wrapper function to run app binary as app user
{config.app_name}() {{
    sudo -u {config.app_user} {app_dir}/.venv/bin/{config.app_name} "$@"
}}
export -f {config.app_name}
""")

        distfile_path = bundle_dir / config.distfile_name
        run(
            f"{uv_python_install_dir} uv venv -p {config.python_version} --managed-python --clear"
        )

        dist_install = f"UV_COMPILE_BYTECODE=1 {uv_python_install_dir} uv pip install {distfile_path}"
        if config.requirements:
            requirements_path = bundle_dir / "requirements.txt"
            run(f"{dist_install} --no-deps && uv pip install -r {requirements_path} ")
        else:
            run(dist_install)
    else:
        log("Installing binary...")
        (app_dir / ".appenv").write_text(f"""set -a
source .env
set +a
export PATH="{app_dir}:$PATH"

# Wrapper function to run app binary as app user
{config.app_name}() {{
    sudo -u {config.app_user} {app_dir}/{config.app_name} "$@"
}}
export -f {config.app_name}
""")
        full_path_app_bin = app_dir / config.app_bin
        full_path_app_bin.unlink(missing_ok=True)
        full_path_app_bin.write_bytes((bundle_dir / config.distfile_name).read_bytes())
        full_path_app_bin.chmod(0o755)

    (app_dir / ".version").write_text(config.version)

    log("Setting file ownership and permissions...")
    run(f"sudo chown -R {config.deploy_user}:{config.app_user} {app_dir}")
    # Make app directory group-writable (for database, logs, etc.)
    run(f"sudo chmod 775 {app_dir}")
    run(f"sudo chmod 640 {app_dir}/.env")

    # .venv permissions: readable/executable by group, writable by owner
    if (app_dir / ".venv").exists():
        run(f"sudo find {app_dir}/.venv -type d -exec chmod 755 {{}} +")
        run(f"sudo find {app_dir}/.venv -type f -exec chmod 644 {{}} +")
        run(f"sudo find {app_dir}/.venv/bin -type f -exec chmod 755 {{}} +")

    os.chdir(bundle_dir)

    log("Configuring systemd services...")
    systemd_dir = bundle_dir / "systemd"

    valid_units = []
    for unit in config.deployed_units:
        valid_units.append(unit["template_service_name"])
        if unit["template_socket_name"]:
            valid_units.append(unit["template_socket_name"])
        if unit["template_timer_name"]:
            valid_units.append(unit["template_timer_name"])

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
    for search_dir in [SYSTEMD_SYSTEM_DIR, SYSTEMD_WANTS_DIR]:
        if not search_dir.exists():
            continue
        for file_path in search_dir.glob(f"{config.app_name}*"):
            if file_path.is_file() and file_path.name not in valid_units:
                print(f"→ Removing stale file: {file_path}")
                run(f"sudo rm -f {file_path}")

    log("Cleaning up stale dropin directories...")
    # Build set of dropin files that should exist after deployment
    expected_dropins = set()

    # Common dropins (applied to all services)
    common_dir = systemd_dir / "common.d"
    if common_dir.exists():
        common_dropin_names = {f.name for f in common_dir.glob("*.conf")}
        for unit in config.deployed_units:
            deployed_service = unit["template_service_name"]
            for dropin_name in common_dropin_names:
                expected_dropins.add(f"{deployed_service}.d/{dropin_name}")

    # Service-specific dropins
    for service_dropin_dir_path in systemd_dir.glob("*.service.d"):
        service_file_name = service_dropin_dir_path.name.removesuffix(".d")
        for unit in config.deployed_units:
            if unit["service_file"] == service_file_name:
                deployed_service = unit["template_service_name"]
                for dropin_path in service_dropin_dir_path.glob("*.conf"):
                    expected_dropins.add(f"{deployed_service}.d/{dropin_path.name}")
                break

    # Remove stale dropin files and empty directories
    for dropin_dir in SYSTEMD_SYSTEM_DIR.glob(f"{config.app_name}*.d"):
        if dropin_dir.is_dir():
            service_name = dropin_dir.name
            for dropin_file in dropin_dir.glob("*.conf"):
                dropin_path_str = f"{service_name}/{dropin_file.name}"
                if dropin_path_str not in expected_dropins:
                    print(f"→ Removing stale dropin: {dropin_file}")
                    run(f"sudo rm -f {dropin_file}")

            try:
                # Check if directory is empty (will fail if not)
                result = run(
                    f'[ -z "$(ls -A {dropin_dir})" ] && echo empty || echo not_empty',
                    capture_output=True,
                    text=True,
                )
                if "empty" in result.stdout:
                    print(f"→ Removing empty dropin directory: {dropin_dir}")
                    run(f"sudo rmdir {dropin_dir}")
            except subprocess.CalledProcessError:
                pass  # Directory not empty, keep it

    log("Installing new service files...")
    for unit in config.deployed_units:
        service_file = systemd_dir / unit["service_file"]
        content = service_file.read_text()
        deployed_path = SYSTEMD_SYSTEM_DIR / unit["template_service_name"]
        run(
            f"sudo tee {deployed_path} > /dev/null",
            input=content,
            text=True,
            check=True,
        )

        if unit["socket_file"]:
            socket_file = systemd_dir / unit["socket_file"]
            socket_content = socket_file.read_text()
            socket_deployed_path = SYSTEMD_SYSTEM_DIR / unit["template_socket_name"]
            run(
                f"sudo tee {socket_deployed_path} > /dev/null",
                input=socket_content,
                text=True,
                check=True,
            )

        if unit["timer_file"]:
            timer_file = systemd_dir / unit["timer_file"]
            timer_content = timer_file.read_text()
            timer_deployed_path = SYSTEMD_SYSTEM_DIR / unit["template_timer_name"]
            run(
                f"sudo tee {timer_deployed_path} > /dev/null",
                input=timer_content,
                text=True,
                check=True,
            )

    # Deploy common dropins (apply to all services)
    common_dir = systemd_dir / "common.d"
    if common_dir.exists():
        for dropin_path in common_dir.glob("*.conf"):
            dropin_content = dropin_path.read_text()

            for unit in config.deployed_units:
                deployed_service = unit["template_service_name"]
                dropin_dir = SYSTEMD_SYSTEM_DIR / f"{deployed_service}.d"
                run(f"sudo mkdir -p {dropin_dir}")
                dropin_dest = dropin_dir / dropin_path.name
                run(
                    f"sudo tee {dropin_dest} > /dev/null",
                    input=dropin_content,
                    text=True,
                    check=True,
                )

    # Deploy service-specific dropins
    for service_dropin_dir_path in systemd_dir.glob("*.service.d"):
        service_file_name = service_dropin_dir_path.name.removesuffix(".d")

        matching_unit = None
        for unit in config.deployed_units:
            if unit["service_file"] == service_file_name:
                matching_unit = unit
                break

        if matching_unit:
            deployed_dropin_dir = (
                SYSTEMD_SYSTEM_DIR / f"{matching_unit['template_service_name']}.d"
            )
            run(f"sudo mkdir -p {deployed_dropin_dir}")

            for dropin_path in service_dropin_dir_path.glob("*.conf"):
                dropin_content = dropin_path.read_text()
                dropin_dest = deployed_dropin_dir / dropin_path.name
                run(
                    f"sudo tee {dropin_dest} > /dev/null",
                    input=dropin_content,
                    text=True,
                    check=True,
                )

    log("Restarting services...")
    active_units = []
    for unit in config.deployed_units:
        active_units.extend(unit["instance_service_names"])
        # Add sockets/timers (not templates, they're activated via instances)
        if not unit["is_template"]:
            if unit["template_socket_name"]:
                active_units.append(unit["template_socket_name"])
            if unit["template_timer_name"]:
                active_units.append(unit["template_timer_name"])
        else:
            # Template sockets/timers use @ without instance number
            if unit["template_socket_name"]:
                active_units.append(unit["template_socket_name"])
            if unit["template_timer_name"]:
                active_units.append(unit["template_timer_name"])

    units_str = " ".join(active_units)
    run(
        f"sudo systemctl daemon-reload && sudo systemctl enable {units_str}",
        check=True,
    )

    restart_result = run(
        f"sudo systemctl restart {units_str}",
        check=False,
    )

    if restart_result.returncode != 0:
        log("⚠️ Services restart failed! Fetching recent logs...")
        for unit in active_units:
            status_result = run(
                f"sudo systemctl is-active {unit}",
                check=False,
                capture_output=True,
                text=True,
            )
            if status_result.stdout.strip() != "active":
                print(f"\n{'=' * 60}")
                print(f"❌ {unit} failed to start")
                print(f"{'=' * 60}")
                # Show last 30 lines of logs for this unit
                run(
                    f"sudo journalctl -u {unit} -n 30 --no-pager",
                    check=False,
                )
        print(f"\n{'=' * 60}")
        print("Deployment failed: One or more services failed to start")
        print(f"{'=' * 60}\n")
        sys.exit(1)

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

    # Build list of units from deployed_units
    valid_units = []
    for unit in config.deployed_units:
        valid_units.append(unit["template_service_name"])
        if unit["template_socket_name"]:
            valid_units.append(unit["template_socket_name"])
        if unit["template_timer_name"]:
            valid_units.append(unit["template_timer_name"])

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
        run(f"sudo rm -f {SYSTEMD_SYSTEM_DIR / unit}")

    run("sudo systemctl daemon-reload && sudo systemctl reset-failed", check=False)

    if config.webserver_enabled:
        log("Removing Caddy configuration...")
        run(f"sudo rm -f {config.caddy_config_path} && sudo systemctl reload caddy")

    log("Deleting app user...")
    user_exists = (
        run(f"id -u {config.app_user} >/dev/null 2>&1", check=False).returncode == 0
    )
    if user_exists:
        # Kill any remaining processes owned by the app user before deletion
        log(f"Terminating processes owned by {config.app_user}...")
        run(f"sudo pkill -u {config.app_user}", check=False)

        # Wait briefly for processes to terminate gracefully
        time.sleep(1)

        # Force kill any stubborn processes
        run(f"sudo pkill -9 -u {config.app_user}", check=False)

        # Now delete the user
        run(f"sudo userdel {config.app_user}", check=False)
    else:
        print(f"User {config.app_user} does not exist, skipping deletion")

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
