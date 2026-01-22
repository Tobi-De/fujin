from __future__ import annotations

from typing import Annotated
from dataclasses import dataclass
from datetime import datetime
from collections import defaultdict

import cappa
from rich.console import Console
from rich.markup import escape

from fujin.audit import read_logs
from fujin.commands import BaseCommand


@cappa.command(
    help="View audit logs for deployment operations",
)
@dataclass
class Audit(BaseCommand):
    limit: Annotated[
        int,
        cappa.Arg(
            short="-n",
            long="--limit",
            help="Number of audit entries to show",
        ),
    ] = 20

    def __call__(self):
        with self.connection() as conn:
            records = read_logs(
                connection=conn,
                app_name=self.config.app_name,
                limit=self.limit,
            )

        if not records:
            console = Console()
            console.print("[dim]No audit logs found[/dim]")
            return

        grouped: dict[str, list[dict]] = defaultdict(list)
        for record in records:
            host = record.get("host", "unknown")
            grouped[host].append(record)

        console = Console()

        first = True
        for host, host_records in grouped.items():
            if not first:
                console.print()
            console.print(f"[green]{host}[/green]:")
            first = False

            for record in host_records:
                try:
                    ts = datetime.fromisoformat(record["timestamp"])
                    timestamp = ts.strftime("%Y-%m-%d %H:%M")
                except (ValueError, KeyError):
                    timestamp = record.get("timestamp", "unknown")

                user = record.get("user", "unknown")
                operation = record.get("operation", "unknown")
                app_name = record.get("app_name", "")

                if operation == "deploy":
                    version = record.get("version", "unknown")
                    git_commit = record.get("git_commit")
                    message = f"Deployed {app_name} version [blue]{version}[/blue]"
                    if git_commit:
                        message += f" [dim]({git_commit[:7]})[/dim]"
                elif operation == "rollback":
                    from_v = record.get("from_version", "unknown")
                    to_v = record.get("to_version", "unknown")
                    message = f"Rolled back {app_name} from [blue]{from_v}[/blue] to [blue]{to_v}[/blue]"
                elif operation == "down":
                    version = record.get("version", "unknown")
                    message = f"Stopped {app_name} version [blue]{version}[/blue]"
                elif operation == "full-down":
                    version = record.get("version", "unknown")
                    message = f"Stopped {app_name} version [blue]{version}[/blue] (full cleanup)"
                else:
                    message = f"{operation}"

                console.print(
                    f"  [{escape(timestamp)}] [dim]\\[[/dim][yellow]{user}[/yellow][dim]][/dim] {message}",
                    highlight=False,
                )
