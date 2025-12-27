from __future__ import annotations

from typing import Annotated
from datetime import datetime
from collections import defaultdict

import cappa
from rich.console import Console
from rich.markup import escape

from fujin.audit import read_logs


@cappa.command(
    help="View local audit logs for deployment operations",
)
class Audit:
    limit: Annotated[
        int,
        cappa.Arg(
            short="-n",
            long="--limit",
            help="Number of audit entries to show",
        ),
    ] = 20

    def __call__(self):
        records = read_logs(limit=self.limit)

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
                details = record.get("details", {})
                app_name = details.get("app_name", "")

                if operation == "deploy":
                    version = details.get("version", "unknown")
                    message = f"Deployed {app_name} version [blue]{version}[/blue]"
                elif operation == "rollback":
                    from_v = details.get("from_version", "unknown")
                    to_v = details.get("to_version", "unknown")
                    message = f"Rolled back {app_name} from [blue]{from_v}[/blue] to [blue]{to_v}[/blue]"
                elif operation == "down":
                    version = details.get("version", "unknown")
                    full = details.get("full", False)
                    full_str = " (full cleanup)" if full else ""
                    message = (
                        f"Stopped {app_name} version [blue]{version}[/blue]{full_str}"
                    )
                else:
                    message = f"{operation}"

                console.print(
                    f"  [{escape(timestamp)}] [dim]\\[[/dim][yellow]{user}[/yellow][dim]][/dim] {message}",
                    highlight=False,
                )
