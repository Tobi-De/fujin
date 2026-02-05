from __future__ import annotations

import argparse
import grp
import json
import logging
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass
from itertools import chain
from pathlib import Path
from typing import Literal, TypedDict

SYSTEMD_SYSTEM_DIR = Path("/etc/systemd/system")
SYSTEMD_WANTS_DIR = SYSTEMD_SYSTEM_DIR / "multi-user.target.wants"

# Exit codes for the installer
EXIT_SUCCESS = 0
EXIT_GENERAL_ERROR = 1
EXIT_VALIDATION_ERROR = 2
EXIT_SERVICE_START_FAILED = 3

logger = logging.getLogger("fujin.installer")


class DeployedUnit(TypedDict):
    """Type hint for deployed unit serialized as dict with all derived properties."""

    name: str  # Base name, e.g., "web"
    service_file: str  # filename only
    socket_file: str | None
    timer_file: str | None
    replicas: int
    is_template: bool
    service_instances: list[str]
    template_service_name: str
    template_socket_name: str | None
    template_timer_name: str | None


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
    deployed_units: list[DeployedUnit]

    @property
    def uv_path(self) -> str:
        """Return full path to uv binary based on deploy user's home directory.

        Using the full path ensures reliability even if PATH is not properly set
        during the installation process. The uv installer places the binary at
        ~/.local/bin/uv by default.
        """
        return f"/home/{self.deploy_user}/.local/bin/uv"


class _InstallerFormatter(logging.Formatter):
    FORMATS = {
        logging.DEBUG: "    → %(message)s",
        logging.INFO: "==> %(message)s",
        logging.WARNING: "⚠️  %(message)s",
        logging.ERROR: "❌  %(message)s",
    }

    def format(self, record: logging.LogRecord) -> str:
        fmt = self.FORMATS.get(record.levelno, "%(message)s")
        return logging.Formatter(fmt).format(record)


def _setup_logging(verbose: int) -> None:
    """Configure logging for the installer."""
    if verbose == 0:
        level = logging.WARNING
    elif verbose == 1:
        level = logging.INFO
    else:
        level = logging.DEBUG

    handler = logging.StreamHandler()
    handler.setFormatter(_InstallerFormatter())
    logger.addHandler(handler)
    logger.setLevel(level)


def install(
    config: InstallConfig, bundle_dir: Path, *, full_restart: bool = False
) -> None:
    """Install the application.
    Assumes it's running from a directory with extracted bundle files.
    """

    # ==========================================================================
    # PHASE 1: DIRECTORY SETUP
    # ==========================================================================
    logger.info("Setting up directories and app user...")
    try:
        pwd.getpwnam(config.app_user)
        logger.debug("User %s already exists", config.app_user)
    except KeyError:
        logger.debug("Creating system user: %s", config.app_user)
        run(
            f"useradd --system --no-create-home --shell /usr/sbin/nologin {config.app_user}",
        )

    app_dir = Path(config.app_dir)
    app_dir.mkdir(parents=True, exist_ok=True)
    logger.debug("Created app directory: %s", app_dir)

    install_dir = app_dir / ".install"
    install_dir.mkdir(exist_ok=True)

    # Move .env file to .install/
    env_file = bundle_dir / ".env"
    if env_file.exists():
        env_file = env_file.rename(install_dir / ".env")
        logger.debug("Moved .env to %s", install_dir / ".env")

    # ==========================================================================
    # PHASE 2: INSTALLATION
    # ==========================================================================
    logger.info("Installing application...")
    os.chdir(install_dir)

    # record app version
    (install_dir / ".version").write_text(config.version)
    logger.debug("Recorded version: %s", config.version)

    service_helpers = _format_service_helpers(config)
    if config.installation_mode == "python-package":
        logger.debug("Installation mode: python-package")

        uv_python_install_dir = "UV_PYTHON_INSTALL_DIR=/opt/fujin/.python"

        (install_dir / ".appenv").write_text(f"""set -a
source {install_dir}/.env
set +a
export {uv_python_install_dir}
export PATH="{install_dir}/.venv/bin:$PATH"

# Wrapper function to run app binary as app user
{config.app_name}() {{
    sudo -u {config.app_user} {install_dir}/.venv/bin/{config.app_name} "$@"
}}
export -f {config.app_name}
{service_helpers}
""")

        distfile_path = bundle_dir / config.distfile_name
        venv_path = install_dir / ".venv"
        if not venv_path.exists():
            logger.debug(
                "Creating virtual environment with Python %s", config.python_version
            )
            run(
                f"{uv_python_install_dir} {config.uv_path} venv -p {config.python_version} --managed-python",
            )
        else:
            logger.debug("Virtual environment already exists")

        logger.debug("Installing package: %s", config.distfile_name)
        dist_install = f"UV_COMPILE_BYTECODE=1 {uv_python_install_dir} {config.uv_path} pip install {distfile_path}"
        if config.requirements:
            requirements_path = bundle_dir / "requirements.txt"
            logger.debug("Installing with requirements file")
            run(
                f"{dist_install} --no-deps && {config.uv_path} pip install -r {requirements_path} ",
            )
        else:
            run(dist_install)

    else:
        logger.debug("Installation mode: binary")
        (install_dir / ".appenv").write_text(f"""set -a
source {install_dir}/.env
set +a
export PATH="{install_dir}:$PATH"

# Wrapper function to run app binary as app user
{config.app_name}() {{
    sudo -u {config.app_user} {install_dir}/{config.app_name} "$@"
}}
export -f {config.app_name}
{service_helpers}
""")
        full_path_app_bin = install_dir / config.app_bin
        full_path_app_bin.unlink(missing_ok=True)
        full_path_app_bin.write_bytes((bundle_dir / config.distfile_name).read_bytes())
        full_path_app_bin.chmod(0o755)
        logger.debug("Installed binary: %s", full_path_app_bin)

    logger.info("Setting file ownership and permissions...")
    logger.debug("Setting ownership to %s:%s", config.deploy_user, config.app_user)
    # Only chown the .install directory - leave app runtime data untouched
    run(f"chown -R {config.deploy_user}:{config.app_user} {install_dir}")
    # Make .install directory group-writable (deploy user can update, app user can read)
    install_dir.chmod(0o775)
    env_file.chmod(0o640)

    # .venv permissions: readable/executable by group, writable by owner
    if (install_dir / ".venv").exists():
        logger.debug("Setting venv permissions")
        run(f"find {install_dir}/.venv -type d -exec chmod 755 {{}} +")
        run(f"find {install_dir}/.venv -type f -exec chmod 644 {{}} +")
        run(f"find {install_dir}/.venv/bin -type f -exec chmod 755 {{}} +")
    # Ensure app_dir itself is group-writable so app can create files
    run(f"chown {config.deploy_user}:{config.app_user} {app_dir}")
    app_dir.chmod(0o775)

    # ==========================================================================
    # PHASE 3: CONFIGURING SYSTEMD SERVICES
    # ==========================================================================

    logger.info("Configuring systemd services...")
    systemd_dir = bundle_dir / "systemd"

    valid_units = []
    for unit in config.deployed_units:
        valid_units.append(unit["template_service_name"])
        if unit["template_socket_name"]:
            valid_units.append(unit["template_socket_name"])
        if unit["template_timer_name"]:
            valid_units.append(unit["template_timer_name"])

    installed_units = [
        f.name for f in SYSTEMD_SYSTEM_DIR.glob(f"{config.app_name}*") if f.is_file()
    ]
    logger.debug("Found %d existing unit files", len(installed_units))

    # Clean up stale units
    stale_units = [u for u in installed_units if u not in valid_units]
    if stale_units:
        logger.debug("Removing %d stale units", len(stale_units))
        for unit in stale_units:
            cmd = f"systemctl disable {unit} --quiet"
            if not unit.endswith("@.service"):
                cmd += " --now"
            logger.debug("Disabling stale unit: %s", unit)
            run(cmd)
            run(f"systemctl reset-failed {unit}", capture_output=True)

    for search_dir in [SYSTEMD_SYSTEM_DIR, SYSTEMD_WANTS_DIR]:
        if not search_dir.exists():
            continue
        for file_path in search_dir.glob(f"{config.app_name}*"):
            if file_path.is_file() and file_path.name not in valid_units:
                logger.debug("Removing stale file: %s", file_path.name)
                file_path.unlink(missing_ok=True)

    for dropin_dir in SYSTEMD_SYSTEM_DIR.glob(f"{config.app_name}*.d"):
        logger.debug("Removing stale dropin directory: %s", dropin_dir.name)
        shutil.rmtree(dropin_dir)

    # Install new service files
    logger.debug("Installing %d service units", len(config.deployed_units))
    for unit in config.deployed_units:
        service_file = systemd_dir / unit["service_file"]
        content = service_file.read_text()
        deployed_path = SYSTEMD_SYSTEM_DIR / unit["template_service_name"]
        deployed_path.write_text(content)
        logger.debug("Wrote %s", deployed_path.name)

        if unit["socket_file"]:
            socket_file = systemd_dir / unit["socket_file"]
            socket_content = socket_file.read_text()
            socket_deployed_path = SYSTEMD_SYSTEM_DIR / unit["template_socket_name"]
            socket_deployed_path.write_text(socket_content)
            logger.debug("Wrote %s", socket_deployed_path.name)

        if unit["timer_file"]:
            timer_file = systemd_dir / unit["timer_file"]
            timer_content = timer_file.read_text()
            timer_deployed_path = SYSTEMD_SYSTEM_DIR / unit["template_timer_name"]
            timer_deployed_path.write_text(timer_content)
            logger.debug("Wrote %s", timer_deployed_path.name)

    # Deploy common dropins (apply to all services)
    common_dir = systemd_dir / "common.d"
    if common_dir.exists():
        common_dropins = list(common_dir.glob("*.conf"))
        if common_dropins:
            logger.debug("Deploying %d common dropins", len(common_dropins))
        for dropin_path in common_dropins:
            dropin_content = dropin_path.read_text()
            for unit in config.deployed_units:
                dropin_dir = SYSTEMD_SYSTEM_DIR / f"{unit['template_service_name']}.d"
                dropin_dir.mkdir(parents=True, exist_ok=True)
                dropin_dest = dropin_dir / dropin_path.name
                dropin_dest.write_text(dropin_content)
                logger.debug(
                    "Wrote common dropin %s to %s",
                    dropin_path.name,
                    dropin_dir.name,
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
            deployed_dropin_dir.mkdir(exist_ok=True, parents=True)
            dropins = list(service_dropin_dir_path.glob("*.conf"))
            logger.debug(
                "Deploying %d dropins for %s",
                len(dropins),
                matching_unit["template_service_name"],
            )
            for dropin_path in dropins:
                dropin_content = dropin_path.read_text()
                dropin_dest = deployed_dropin_dir / dropin_path.name
                dropin_dest.write_text(dropin_content)
                logger.debug("Wrote dropin %s", dropin_path.name)

    logger.info("Restarting services...")
    active_units = []
    for unit in config.deployed_units:
        active_units.extend(unit["service_instances"])
        if unit["template_socket_name"]:
            active_units.append(unit["template_socket_name"])
        if unit["template_timer_name"]:
            active_units.append(unit["template_timer_name"])

    units_str = " ".join(active_units)
    run(
        f"systemctl daemon-reload && systemctl enable {units_str}",
        check=True,
    )

    restart_cmd = "restart" if full_restart else "reload-or-restart"
    restart_result = run(
        f"systemctl {restart_cmd} {units_str}",
    )

    # Wait briefly for services to stabilize - services that crash immediately
    # may appear "active" right after restart before systemd detects the failure
    time.sleep(2)

    # Check if services are actually running (not just restart command succeeded)
    # only check services with no timer or socket, they are the only one that should run immediatly
    units_to_check = [
        unit["service_instances"]
        for unit in config.deployed_units
        if not (unit["template_socket_name"] or unit["template_timer_name"])
    ]
    units_to_check = list(chain.from_iterable(units_to_check))
    failed_units = []
    for unit in units_to_check:
        status_result = run(
            f"systemctl is-active {unit}",
            capture_output=True,
        )
        if status_result.stdout.strip() != "active":
            failed_units.append(unit)

    if restart_result.returncode != 0 or failed_units:
        logger.error("Services failed to start!")
        for unit in failed_units:
            logger.error("")
            logger.error("=" * 60)
            logger.error("%s failed to start", unit)
            logger.error("=" * 60)
            # This checks for syntax errors or missing dependencies defined in the unit file
            unit_path = SYSTEMD_SYSTEM_DIR / unit
            if unit_path.exists():
                import shlex

                logger.error("Checking systemd unit configuration...")
                # Always show output for failed services - don't suppress
                subprocess.run(
                    f"systemd-analyze verify {shlex.quote(str(unit_path))}",
                    shell=True,
                )
            else:
                logger.error("Unit file not found at %s", unit_path)

            # Show last 30 lines of logs for this unit
            logger.error("Recent logs:")
            subprocess.run(
                f"journalctl -u {unit} -n 30 --no-pager",
                shell=True,
            )
        sys.exit(EXIT_SERVICE_START_FAILED)

    # ==========================================================================
    # PHASE 4: CADDY CONFIGURATION
    # ==========================================================================
    # Configure Caddy after services are running successfully
    if config.webserver_enabled:
        caddyfile_path = bundle_dir / "Caddyfile"
        if caddyfile_path.exists():
            logger.info("Configuring Caddy...")
            run(f"usermod -aG {config.app_user} caddy")

            caddy_config_path = Path(config.caddy_config_path)

            # Backup existing config if it exists
            old_config_content = None
            if caddy_config_path.exists():
                old_config_content = caddy_config_path.read_text()

            # Copy new config
            shutil.copy2(caddyfile_path, caddy_config_path)
            uid = pwd.getpwnam("caddy").pw_uid
            gid = grp.getgrnam("caddy").gr_gid
            os.chown(caddy_config_path, uid, gid)

            logger.debug("Reloading Caddy")
            try:
                reload_result = run(
                    "systemctl reload caddy",
                    timeout=20,
                    capture_output=True,
                )
                reload_failed = reload_result.returncode != 0
            except subprocess.TimeoutExpired:
                reload_failed = True
                logger.warning("Caddy reload timeout")

            if reload_failed:
                logger.warning("Caddy reload failed")
                # Always show Caddy logs on failure - don't suppress
                logger.warning("Recent Caddy logs:")
                subprocess.run(
                    "journalctl -u caddy.service -n 15 --no-pager",
                    shell=True,
                )

                if old_config_content:
                    logger.warning("Restoring previous Caddy configuration")
                    caddy_config_path.write_text(old_config_content)
                else:
                    logger.warning("Removing invalid Caddy configuration")
                    caddy_config_path.unlink(missing_ok=True)

                logger.warning(
                    "App is running but Caddy configuration failed. "
                    "Fix your Caddyfile and redeploy."
                )
            else:
                logger.debug("Caddy configuration updated and reloaded")

    logger.info("Install completed successfully.")


def uninstall(config: InstallConfig, bundle_dir: Path) -> None:
    """Uninstall the application.

    Assumes it's running from a directory with extracted bundle files.
    """
    logger.info("Uninstalling %s...", config.app_name)

    regular_units = []
    template_units = []
    for unit in config.deployed_units:
        target = template_units if unit["is_template"] else regular_units
        target.append(unit["template_service_name"])
        if unit["template_socket_name"]:
            target.append(unit["template_socket_name"])
        if unit["template_timer_name"]:
            target.append(unit["template_timer_name"])

    valid_units = regular_units + template_units

    logger.info("Stopping services...")
    logger.debug("Disabling %d units", len(valid_units))
    if regular_units:
        run(
            f"systemctl disable --now {' '.join(regular_units)} --quiet",
        )
    if template_units:
        run(f"systemctl disable {' '.join(template_units)} --quiet")

    logger.debug("Removing systemd unit files")
    for unit in valid_units:
        if not unit.startswith(config.app_name):
            logger.error("Refusing to remove non-app unit: %s", unit)
            continue
        (SYSTEMD_SYSTEM_DIR / unit).unlink(missing_ok=True)
        logger.debug("Removed %s", unit)

    run("systemctl daemon-reload && systemctl reset-failed")

    if config.webserver_enabled:
        logger.info("Removing Caddy configuration...")
        Path(config.caddy_config_path).unlink(missing_ok=True)
        logger.debug("Reloading Caddy")
        run("systemctl reload caddy")
        logger.debug("Removing caddy from %s group", config.app_user)
        run(f"gpasswd -d caddy {config.app_user}")

    logger.info("Deleting app user...")
    try:
        pwd.getpwnam(config.app_user)
    except KeyError:
        logger.debug("User %s does not exist, skipping", config.app_user)
    else:
        logger.debug("Terminating processes owned by %s", config.app_user)
        run(f"pkill -u {config.app_user}")
        time.sleep(1)
        run(f"pkill -9 -u {config.app_user}")
        logger.debug("Deleting user %s", config.app_user)
        run(f"userdel {config.app_user}")

    logger.info("Uninstall completed.")


def run(
    cmd: str,
    *,
    check: bool = False,
    capture_output: bool = False,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a shell command with verbosity-aware output."""
    is_debug = logger.level <= logging.DEBUG

    if is_debug and not capture_output:
        logger.debug("Running: %s", cmd)

    kwargs = {"shell": True, "check": check, "timeout": timeout}

    if capture_output:
        kwargs.update(capture_output=True, text=True)
    elif not is_debug:
        kwargs.update(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    return subprocess.run(cmd, **kwargs)


def main() -> None:
    """Main entry point.

    Handles extraction of zipapp to temp directory and cleanup.
    """

    parser = argparse.ArgumentParser(
        prog="installer.pyz",
        description="Fujin application installer",
    )
    parser.add_argument(
        "command",
        choices=["install", "uninstall"],
        help="Command to run",
    )
    parser.add_argument(
        "--full-restart",
        action="store_true",
        help="Force a full restart instead of reload-or-restart",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        type=int,
        default=0,
        help="Verbosity level (0=warning, 1=info, 2+=debug)",
    )

    args = parser.parse_args()

    _setup_logging(args.verbose)

    source_path = Path(__file__).parent
    zipapp_file = str(source_path)

    with tempfile.TemporaryDirectory(
        prefix=f"fujin-{args.command}-{source_path.name}"
    ) as tmpdir:
        try:
            logger.debug("Extracting installer bundle...")
            with zipfile.ZipFile(zipapp_file, "r") as zf:
                zf.extractall(tmpdir)

            # Change to temp directory and run command
            original_dir = os.getcwd()
            os.chdir(tmpdir)

            bundle_dir = Path(tmpdir)
            config_path = bundle_dir / "config.json"
            config = InstallConfig(**json.loads(config_path.read_text()))
            try:
                if args.command == "install":
                    install(config, bundle_dir, full_restart=args.full_restart)
                else:
                    uninstall(config, bundle_dir)
            finally:
                os.chdir(original_dir)

        except Exception as e:
            logger.error("ERROR: %s failed: %s", args.command, e)
            import traceback

            traceback.print_exc()
            sys.exit(1)


def _format_service_helpers(config: InstallConfig) -> str:
    """Format service management helpers with config values."""
    valid_services = " ".join(u["name"] for u in config.deployed_units)
    return service_management_helpers.format(
        app_name=config.app_name,
        app_user=config.app_user,
        valid_services=valid_services,
    )


service_management_helpers = """
export VALID_SERVICES="{valid_services}"

_validate_svc() {{
    local svc="$1"
    [[ "$svc" == "*" ]] && return 0
    for s in $VALID_SERVICES; do
        [[ "$svc" == "$s" ]] && return 0
    done
    echo "Error: Service '$svc' not found. Available: $VALID_SERVICES" >&2
    return 1
}}
export -f _validate_svc

_svc() {{
    local cmd="$1"
    local svc="${{2:-*}}"
    _validate_svc "$svc" || return 1
    # Use glob to match both regular and template instances
    local pattern="{app_name}-${{svc}}*.service"
    local units=$(systemctl list-units --type=service --no-legend "$pattern" 2>/dev/null | awk '{{print $1}}')
    [[ -z "$units" ]] && units="{app_name}-${{svc}}.service"
    case "$cmd" in
        status) sudo systemctl status $units --no-pager ;;
        *) sudo systemctl "$cmd" $units ;;
    esac
}}
export -f _svc

status() {{ _svc status "$1"; }}
export -f status
start() {{ _svc start "$1"; }}
export -f start
stop() {{ _svc stop "$1"; }}
export -f stop
restart() {{ _svc restart "$1"; }}
export -f restart

logs() {{
    local svc="${{1:-*}}"
    _validate_svc "$svc" || return 1
    # Use glob to match both regular and template instances
    local pattern="{app_name}-${{svc}}*.service"
    local units=$(systemctl list-units --type=service --no-legend "$pattern" 2>/dev/null | awk '{{print $1}}')
    [[ -z "$units" ]] && units="{app_name}-${{svc}}.service"
    local unit_args=$(echo $units | sed 's/[^ ]* */-u &/g')
    sudo journalctl $unit_args -f
}}
export -f logs

logtail() {{
    local lines="${{1:-100}}"
    local svc="${{2:-*}}"
    _validate_svc "$svc" || return 1
    local pattern="{app_name}-${{svc}}*.service"
    local units=$(systemctl list-units --type=service --no-legend "$pattern" 2>/dev/null | awk '{{print $1}}')
    [[ -z "$units" ]] && units="{app_name}-${{svc}}.service"
    local unit_args=$(echo $units | sed 's/[^ ]* */-u &/g')
    sudo journalctl $unit_args -n "$lines" --no-pager
}}
export -f logtail

procs() {{
    ps aux | grep -E "({app_name}|{app_user})" | grep -v grep
}}
export -f procs

mem() {{
    ps -u {app_user} -o pid,rss,vsz,comm --sort=-rss 2>/dev/null || echo "No processes found"
}}
export -f mem
"""

if __name__ == "__main__":
    main()
