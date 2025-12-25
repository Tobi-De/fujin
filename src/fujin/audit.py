from __future__ import annotations

import json
import getpass
from pathlib import Path
from datetime import datetime
from typing import Any


def log_operation(
    operation: str,
    host: str,
    details: dict[str, Any] | None = None,
):
    """
    Log an operation to the local audit log.

    Args:
        operation: Type of operation (deploy, rollback, down, etc.)
        host: Target host name or domain
        details: Operation-specific details (version, git_commit, etc.)
    """
    log_dir = Path.home() / ".fujin"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "audit.log"

    record = {
        "timestamp": datetime.now().isoformat(),
        "operation": operation,
        "user": getpass.getuser(),
        "host": host,
    }

    if details:
        record["details"] = details

    # Append to log file as JSON lines
    with log_file.open("a") as f:
        f.write(json.dumps(record) + "\n")


def read_logs(limit: int | None = None) -> list[dict[str, Any]]:
    """
    Read audit logs from the log file.

    Args:
        limit: Maximum number of records to return (most recent first)

    Returns:
        List of audit log records
    """
    log_file = Path.home() / ".fujin" / "audit.log"

    if not log_file.exists():
        return []

    records = []
    with log_file.open("r") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # Skip malformed lines

    # Return most recent first
    records.reverse()

    if limit:
        return records[:limit]
    return records
