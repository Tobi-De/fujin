from __future__ import annotations

import re
from typing import Annotated

import cappa

from fujin.commands import BaseCommand
from fujin.secrets import resolve_secrets


@cappa.command(
    help="Show deployment configuration and rendered templates",
)
class Show(BaseCommand):
    @cappa.command(help="Show rendered systemd units")
    def units(self):
        """Display all systemd unit files that will be deployed."""
        units, user_units = self.config.render_systemd_units()

        if not units and not user_units:
            self.output.warning("No systemd units configured")
            return

        if units:
            for filename, content in units.items():
                self.output.info(f"\n[bold cyan]# {filename}[/bold cyan]")
                self.output.output(content)

        if user_units:
            self.output.info("\n[bold cyan]# User Units[/bold cyan]")
            for filename, content in user_units.items():
                self.output.info(f"\n[bold cyan]# {filename}[/bold cyan]")
                self.output.output(content)

    @cappa.command(help="Show rendered Caddyfile")
    def caddy(self):
        if not self.config.webserver.enabled:
            self.output.warning("Webserver is not enabled in configuration")
            return

        caddyfile = self.config.render_caddyfile()
        self.output.info(
            f"[bold cyan]# Caddyfile for {self.config.host.domain_name}[/bold cyan]"
        )
        self.output.output(caddyfile)

    @cappa.command(help="Show environment variables with resolved secrets")
    def env(
        self,
        plain: Annotated[
            bool,
            cappa.Arg(
                long="--plain",
                help="Show actual secret values instead of redacting them",
            ),
        ] = False,
    ):
        """Display environment variables, optionally with secrets redacted."""
        if not self.config.host.env_content:
            self.output.warning("No environment file configured")
            return

        if self.config.secret_config:
            resolved_env = resolve_secrets(
                self.config.host.env_content, self.config.secret_config
            )
        else:
            resolved_env = self.config.host.env_content

        if not plain:
            resolved_env = _redact_secrets(resolved_env)
            self.output.info(
                "[dim]# Secrets are redacted. Use --plain to show actual values[/dim]"
            )

        self.output.output(resolved_env)


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
