from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Annotated

import cappa

from fujin.commands import BaseCommand
from fujin.formatting import safe_format
from fujin.secrets import resolve_secrets


@cappa.command(
    help="Show deployment configuration and rendered templates",
)
@dataclass
class Show(BaseCommand):
    name: Annotated[
        str | None,
        cappa.Arg(
            help="What to show: process name, unit name, or 'env'/'caddy'/'units'"
        ),
    ] = None
    plain: Annotated[
        bool,
        cappa.Arg(long="--plain", help="Show actual secret values (for 'env')"),
    ] = False

    def __call__(self):
        if not self.name:
            self.output.info("Available options:")
            self.output.output(self._get_available_options())
            return

        if self.name == "env":
            self._show_env(self.plain)
        elif self.name == "caddy":
            self._show_caddy()
        elif self.name == "units":
            self._show_all_units()
        else:
            self._show_specific_units(self.name)

    def _show_all_units(self):
        """Show systemd unit files from .fujin/systemd/."""
        systemd_dir = self.config.local_config_dir / "systemd"
        if not systemd_dir.exists():
            self.output.warning("No systemd directory found in .fujin/")
            return

        # Collect all unit files
        unit_files = []
        for pattern in ["*.service", "*.socket", "*.timer"]:
            unit_files.extend(systemd_dir.glob(pattern))

        # Also collect dropin files
        dropin_files = []
        for dropin_dir in systemd_dir.glob("*.d"):
            dropin_files.extend(dropin_dir.glob("*.conf"))

        all_files = sorted(unit_files + dropin_files)

        if not all_files:
            self.output.warning("No unit files found in .fujin/systemd/")
            return

        separator = "[dim]" + "-" * 80 + "[/dim]"
        first = True
        for file_path in all_files:
            if not first:
                self.output.output(f"\n{separator}\n")
            # Show relative path from systemd dir
            relative_name = file_path.relative_to(systemd_dir)
            self.output.info(f"[bold cyan]# {relative_name}[/bold cyan]")
            self.output.output(file_path.read_text())
            first = False

    def _show_caddy(self):
        if not self.config.caddyfile_exists:
            self.output.warning("No Caddyfile found in .fujin/")
            return

        caddyfile = self.config.caddyfile_path.read_text()
        self.output.info(
            f"[bold cyan]# Caddyfile for {self.config.app_name}[/bold cyan]"
        )
        self.output.output(caddyfile)

    def _show_env(self, plain: bool = False):
        if not self.selected_host.env_content:
            # Check if an envfile was configured but is empty
            if (
                hasattr(self.selected_host, "_env_file")
                and self.selected_host._env_file
            ):
                self.output.warning("Environment file is empty")
            else:
                self.output.warning("No environment file configured")
            return

        if self.config.secret_config:
            resolved_env = resolve_secrets(
                self.selected_host.env_content, self.config.secret_config
            )
        else:
            resolved_env = self.selected_host.env_content

        if not plain:
            resolved_env = _redact_secrets(resolved_env)
            self.output.info(
                "[dim]# Secrets are redacted. Use --plain to show actual values[/dim]"
            )

        self.output.output(resolved_env)

    def _show_specific_units(self, name: str):
        """Display specific unit(s) based on the provided process name."""
        # Discover services
        discovered_services = self.config.discovered_services
        if not discovered_services:
            self.output.warning("No systemd units configured")
            return

        # Filter services matching the requested name
        matching_services = [svc for svc in discovered_services if svc.name == name]

        if not matching_services:
            # Fallback: check if the user asked for a full unit name (e.g. web.service)
            matching_services = [
                svc for svc in discovered_services if svc.service_file.name == name
            ]

        if not matching_services:
            available = [s.name for s in discovered_services]
            raise cappa.Exit(
                f"Unknown target '{name}'. Available options: {', '.join(available)}",
                code=1,
            )

        # Context for rendering
        context = {
            "app_name": self.config.app_name,
            "version": self.config.version,
            "app_dir": self.config.app_dir(self.selected_host),
            "user": self.selected_host.user,
        }

        separator = "[dim]" + "-" * 80 + "[/dim]"
        first = True

        for svc in matching_services:
            # Render and show service file
            if not first:
                self.output.output(f"\n{separator}\n")

            content = safe_format(svc.service_file.read_text(), **context)
            self.output.info(f"[bold cyan]# {svc.service_file.name}[/bold cyan]")
            self.output.output(content)
            first = False

            # Render and show socket file
            if svc.socket_file:
                self.output.output(f"\n{separator}\n")
                content = safe_format(svc.socket_file.read_text(), **context)
                self.output.info(f"[bold cyan]# {svc.socket_file.name}[/bold cyan]")
                self.output.output(content)

            # Render and show timer file
            if svc.timer_file:
                self.output.output(f"\n{separator}\n")
                content = safe_format(svc.timer_file.read_text(), **context)
                self.output.info(f"[bold cyan]# {svc.timer_file.name}[/bold cyan]")
                self.output.output(content)

            # Show associated drop-ins
            service_dropin_dir = (
                self.config.local_config_dir / "systemd" / f"{svc.name}.service.d"
            )
            if svc.is_template:
                service_dropin_dir = (
                    self.config.local_config_dir / "systemd" / f"{svc.name}@.service.d"
                )

            if service_dropin_dir.exists():
                for dropin in service_dropin_dir.glob("*.conf"):
                    self.output.output(f"\n{separator}\n")
                    content = safe_format(dropin.read_text(), **context)
                    self.output.info(
                        f"[bold cyan]# {service_dropin_dir.name}/{dropin.name}[/bold cyan]"
                    )
                    self.output.output(content)

    def _get_available_options(self) -> str:
        """Get list of available options for help text."""
        options = ["env", "caddy", "units"]

        discovered = self.config.discovered_services
        if discovered:
            options.extend([svc.name for svc in discovered])

        return ", ".join(options)


def _redact_secrets(env_content: str) -> str:
    """Redact secret values in environment content."""
    lines = []
    for line in env_content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            lines.append(line)
            continue

        # Match KEY=VALUE or KEY="VALUE"
        match = re.match(r"^([^=]+)=(.*)$", line)
        if match:
            key, value = match.groups()
            # Redact if value looks like a secret (quoted or contains special chars)
            if value and (value.startswith('"') or len(value) > 10):
                lines.append(f'{key}="***REDACTED***"')
            else:
                lines.append(line)
        else:
            lines.append(line)

    return "\n".join(lines)
