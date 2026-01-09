from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import cappa
import msgspec

from fujin.commands import BaseCommand


@cappa.command(help="Migrate fujin.toml to latest configuration format")
@dataclass
class Migrate(BaseCommand):
    backup: Annotated[
        bool,
        cappa.Arg(
            long="--backup",
            help="Create backup of original file (fujin.toml.backup)",
        ),
    ] = True
    dry_run: Annotated[
        bool,
        cappa.Arg(
            long="--dry-run",
            help="Show what would change without writing to file",
        ),
    ] = False

    def __call__(self):
        fujin_toml = Path("fujin.toml")
        if not fujin_toml.exists():
            self.output.error("No fujin.toml file found in the current directory")
            raise cappa.Exit(code=1)

        # Read raw TOML (don't validate yet - it might be in old format)
        try:
            config_dict = msgspec.toml.decode(fujin_toml.read_text())
        except Exception as e:
            self.output.error(f"Failed to parse fujin.toml: {e}")
            raise cappa.Exit(code=1)

        # Apply migrations
        migrated = migrate_config(config_dict)

        # Check if anything changed
        if migrated == config_dict:
            self.output.info("Configuration is already in the latest format")
            return

        # Show changes
        self._show_changes(config_dict, migrated)

        if self.dry_run:
            self.output.info("\n[dim]Dry run - no changes written[/dim]")
            return

        # Create backup if requested
        if self.backup:
            backup_path = Path("fujin.toml.backup")
            shutil.copy2(fujin_toml, backup_path)
            self.output.info(f"Backup created: {backup_path}")

        # Write migrated config
        migrated_toml = msgspec.toml.encode(migrated).decode()
        fujin_toml.write_text(migrated_toml)
        self.output.success("Configuration migrated successfully")

        # Try to validate the new config
        try:
            from fujin.config import Config

            Config.read()
            self.output.success("New configuration is valid")
        except Exception as e:
            self.output.warning(
                f"Migration complete but validation failed: {e}\n"
                "You may need to manually fix the configuration."
            )

    def _show_changes(self, old: dict, new: dict):
        """Show what changed during migration."""
        changes = []

        # Check for single host → hosts array
        if "host" in old and "hosts" in new:
            changes.append("• Converted single 'host' to 'hosts' array")

        # Check for host field renames
        for host in old.get("hosts", [old.get("host", {})]):
            if "ip" in host or "domain_name" in host:
                changes.append(
                    "• Renamed 'ip'/'domain_name' to 'address' in host config"
                )
                break
            if "ssh_port" in host:
                changes.append("• Renamed 'ssh_port' to 'port' in host config")
                break

        # Check for simple process strings
        old_processes = old.get("processes", {})
        if any(isinstance(v, str) for v in old_processes.values()):
            changes.append("• Converted simple process strings to dict format")

        # Check for webserver migration
        if "webserver" in old:
            changes.append("• Migrated 'webserver' config to 'sites' array")
            if old["webserver"].get("upstream"):
                changes.append("• Moved 'webserver.upstream' to 'processes.web.listen'")
            if old["webserver"].get("statics"):
                changes.append("• Converted 'webserver.statics' to static routes")

        if changes:
            self.output.info("[bold]Migration changes:[/bold]")
            for change in changes:
                self.output.output(f"  {change}")


def migrate_config(config_dict: dict) -> dict:
    """
    Migrate old config format to new format.

    Handles:
    - Single host → hosts array
    - host.ip/domain_name → host.address
    - host.ssh_port → host.port
    - Simple process strings → process dicts
    - webserver config → sites array
    - webserver.upstream → processes.web.listen
    - Drop webserver.type if present
    """
    # Work on a copy to avoid mutating the original
    config = dict(config_dict)

    # 1. Single host → hosts array
    if "host" in config and "hosts" not in config:
        config["hosts"] = [config.pop("host")]

    # 2. Extract webserver.upstream before processing (needed for web.listen)
    web_listen = None
    if "webserver" in config:
        web_listen = config["webserver"].get("upstream")

    # 3. Migrate host fields
    for host in config.get("hosts", []):
        # ip or domain_name → address
        if "ip" in host:
            host["address"] = host.pop("ip")
        elif "domain_name" in host:
            host["address"] = host.pop("domain_name")

        # ssh_port → port
        if "ssh_port" in host:
            host["port"] = host.pop("ssh_port")

    # 4. Convert simple process strings → dicts and add listen to web process
    processes = config.get("processes", {})
    for name, value in list(processes.items()):
        if isinstance(value, str):
            # Simple string format: process = "command"
            if name == "web" and web_listen:
                processes[name] = {"command": value, "listen": web_listen}
            else:
                processes[name] = {"command": value}
        elif isinstance(value, dict):
            # Dict format: check if web process needs listen field
            if name == "web" and web_listen and "listen" not in value:
                value["listen"] = web_listen

    # 5. Migrate webserver config to sites
    if "webserver" in config:
        webserver = config.pop("webserver")

        # Drop deprecated type field
        webserver.pop("type", None)

        # Build sites config if not already present
        if "sites" not in config and config.get("hosts"):
            # Get domain from first host address
            domain = config["hosts"][0].get("address", "example.com")

            routes = {}

            # Add static routes from webserver.statics
            for path, directory in webserver.get("statics", {}).items():
                routes[path] = {"static": directory}

            # Add web process route if web process exists
            if "web" in processes:
                routes["/"] = "web"

            if routes:
                config["sites"] = [{"domains": [domain], "routes": routes}]

    return config
