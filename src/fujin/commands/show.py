from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Annotated

import cappa

from fujin.commands import BaseCommand
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
        units, user_units = self.config.render_systemd_units(self.selected_host)

        if not units:
            self.output.warning("No systemd units configured")
            return

        separator = "[dim]" + "-" * 80 + "[/dim]"
        first = True
        for filename, content in units.items():
            if filename not in user_units:
                if not first:
                    self.output.output(f"\n{separator}\n")
                self.output.info(f"[bold cyan]# {filename}[/bold cyan]")
                self.output.output(content)
                first = False

        if user_units:
            if not first:
                self.output.output(f"\n{separator}\n")
            self.output.info("[bold cyan]# User Units[/bold cyan]")
            for filename in user_units:
                self.output.output(f"\n{separator}\n")
                self.output.info(f"[bold cyan]# {filename}[/bold cyan]")
                self.output.output(units[filename])

    def _show_caddy(self):
        if not self.config.sites:
            self.output.warning("No sites configured")
            return

        caddyfile = self.config.render_caddyfile(self.selected_host)
        domains = ", ".join(self.config.sites[0].domains)
        self.output.info(f"[bold cyan]# Caddyfile for {domains}[/bold cyan]")
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
        import cappa

        # Get all rendered units
        units, user_units = self.config.render_systemd_units(self.selected_host)

        if not units:
            self.output.warning("No systemd units configured")
            return

        # Resolve the name to template unit names
        try:
            resolved_names = self._resolve_units(name, use_templates=True)
        except cappa.Exit:
            # Re-raise with colored options
            raise cappa.Exit(
                f"Unknown target '{name}'. Available options: {self._get_available_options()}",
                code=1,
            )

        # Filter to only the requested units
        units_to_show = {
            filename: content
            for filename, content in units.items()
            if filename in resolved_names
        }

        if not units_to_show:
            self.output.warning(f"No units found for '{name}'")
            return

        # Display the units with separators
        separator = "[dim]" + "-" * 80 + "[/dim]"
        first = True
        for filename, content in units_to_show.items():
            if not first:
                self.output.output(f"\n{separator}\n")
            self.output.info(f"[bold cyan]# {filename}[/bold cyan]")
            self.output.output(content)
            first = False


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
