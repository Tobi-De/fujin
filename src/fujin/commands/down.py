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

    def __call__(self):
        try:
            confirm = Confirm.ask(
                f"""[red]You are about to delete all project files, stop all services, 
                and remove all configurations on the host {self.config.host.ip} for the project {self.config.app_name}. 
                Any assets in your project folder will be lost (sqlite not in there ?). 
                Are you sure you want to proceed? This action is irreversible.[/red]""",
            )
        except KeyboardInterrupt:
            raise cappa.Exit("Teardown aborted", code=0)
        if not confirm:
            return

        with self.connection() as conn:
            self.stdout.output("[blue]Tearing down project...[/blue]")
            script = [
                f"APP_DIR={self.config.app_dir}",
                f"APP_NAME={self.config.app_name}",
                'if [ -f "$APP_DIR/.current_version" ]; then',
                '  CURRENT_VERSION=$(cat "$APP_DIR/.current_version")',
                '  CURRENT_BUNDLE="$APP_DIR/.versions/$APP_NAME-$CURRENT_VERSION.tar.gz"',
                '  if [ -f "$CURRENT_BUNDLE" ]; then',
                '    TMP_DIR="/tmp/uninstall-$CURRENT_VERSION"',
                '    mkdir -p "$TMP_DIR"',
                '    tar -xzf "$CURRENT_BUNDLE" -C "$TMP_DIR" uninstall.sh',
                '    bash "$TMP_DIR/uninstall.sh" || true',
                '    rm -rf "$TMP_DIR"',
                "  fi",
                "fi",
                'rm -rf "$APP_DIR"',
            ]
            if self.full and self.config.webserver.enabled:
                script.extend(caddy.get_uninstall_commands())

            conn.run("\n".join(script), warn=True, pty=True)

            self.stdout.output(
                "[green]Project teardown completed successfully![/green]"
            )
