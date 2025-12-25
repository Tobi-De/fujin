from __future__ import annotations

from typing import Annotated
from datetime import datetime

import cappa
from rich.table import Table
from rich.console import Console

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

        # Display in a table
        console = Console()
        table = Table(title="Local Audit Log", show_header=True)
        table.add_column("When", style="green", width=16)
        table.add_column("Operation", style="cyan", width=12)
        table.add_column("Host", style="yellow")
        table.add_column("User", width=12)
        table.add_column("Details", style="dim")

        for record in records:
            # Format timestamp
            try:
                ts = datetime.fromisoformat(record["timestamp"])
                when = ts.strftime("%Y-%m-%d %H:%M")
            except (ValueError, KeyError):
                when = record.get("timestamp", "unknown")

            operation = record.get("operation", "unknown")
            host = record.get("host", "unknown")
            user = record.get("user", "unknown")
            details = record.get("details", {})

            # Format details based on operation
            details_str = ""
            if operation == "deploy":
                version = details.get("version", "")
                details_str = f"v{version}"
            elif operation == "rollback":
                from_v = details.get("from_version", "")
                to_v = details.get("to_version", "")
                details_str = f"{from_v} → {to_v}"
            elif operation == "down":
                version = details.get("version", "")
                full = details.get("full", False)
                details_str = f"v{version}"
                if full:
                    details_str += " (full)"

            table.add_row(when, operation, host, user, details_str)

        console.print(table)
