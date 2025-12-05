from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

import cappa
from rich.prompt import Confirm

from fujin import caddy
from fujin.commands import BaseCommand


@cappa.command(
    help="Tear down the project by stopping services and cleaning up resources"
)
@dataclass
class Down(BaseCommand):
    full: Annotated[
        bool,
        cappa.Arg(
            short="-f",
            long="--full",
            help="Stop and uninstall proxy as part of teardown",
        ),
    ] = False
    force: Annotated[
        bool,
        cappa.Arg(
            long="--force",
            help="Continue teardown even if uninstall script fails",
        ),
    ] = False

    def __call__(self):
        msg = (
            f"[red]You are about to delete all project files, stop all services,\n"
            f"and remove all configurations on the host {self.config.host.ip} for the project {self.config.app_name}.\n"
            f"Any assets in your project folder will be lost.\n"
            f"Are you sure you want to proceed? This action is irreversible.[/red]"
        )
        try:
            confirm = Confirm.ask(msg)
        except KeyboardInterrupt:
            raise cappa.Exit("Teardown aborted", code=0)
        if not confirm:
            return

        with self.connection() as conn:
            self.stdout.output("[blue]Tearing down project...[/blue]")

            script_header = ["#!/usr/bin/env bash"]
            if not self.force:
                script_header.append("set -e")

            script_body = [
                f"APP_DIR={self.config.app_dir}",
                f"APP_NAME={self.config.app_name}",
                'if [ -f "$APP_DIR/.version" ]; then',
                '  CURRENT_VERSION=$(cat "$APP_DIR/.version")',
                '  CURRENT_BUNDLE="$APP_DIR/.versions/$APP_NAME-$CURRENT_VERSION.tar.gz"',
                '  if [ -f "$CURRENT_BUNDLE" ]; then',
                '    TMP_DIR="/tmp/uninstall-$CURRENT_VERSION"',
                '    mkdir -p "$TMP_DIR"',
                '    if tar -xzf "$CURRENT_BUNDLE" -C "$TMP_DIR"; then',
                '      if [ -f "$TMP_DIR/uninstall.sh" ]; then',
                '        echo "Running uninstall script for version $CURRENT_VERSION..."',
                '        bash "$TMP_DIR/uninstall.sh"',
                "      else",
                '        echo "Warning: uninstall.sh not found in bundle."',
                '        if [ -z "$FORCE" ]; then exit 1; fi',
                "      fi",
                "    else",
                '      echo "Warning: Failed to extract bundle."',
                '      if [ -z "$FORCE" ]; then exit 1; fi',
                "    fi",
                '    rm -rf "$TMP_DIR"',
                "  fi",
                "fi",
                'echo "Removing application directory..."',
                'rm -rf "$APP_DIR"',
            ]

            if self.force:
                script_header.append("FORCE=1")

            script = script_header + script_body

            if self.full and self.config.webserver.enabled:
                script.extend(caddy.get_uninstall_commands())

            _, result_ok = conn.run("\n".join(script), warn=True, pty=True)
            if not result_ok:
                if not self.force:
                    self.stdout.output("[red]Teardown failed[/red]")
                    self.stdout.output(
                        "[yellow]Use --force to ignore errors and continue teardown.[/yellow]"
                    )
                    raise cappa.Exit(code=1)
                else:
                    self.stdout.output(
                        "[yellow]Teardown encountered errors but continuing due to --force[/yellow]"
                    )

            self.stdout.output(
                "[green]Project teardown completed successfully![/green]"
            )
