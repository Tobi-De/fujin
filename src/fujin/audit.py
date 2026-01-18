from __future__ import annotations

import json
import getpass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fujin.connection import SSH2Connection


def log_operation(
    connection: SSH2Connection,
    app_name: str,
    operation: str,
    host: str,
    *,
    version: str | None = None,
    git_commit: str | None = None,
    from_version: str | None = None,
    to_version: str | None = None,
):
    """
    Log an operation to the server-side audit log.

    Args:
        connection: SSH connection to the server
        app_name: Application name
        operation: Type of operation (deploy, rollback, down, full-down, etc.)
        host: Target host name or domain
        version: Version being deployed/affected
        git_commit: Git commit hash (for deploy operations)
        from_version: Previous version (for rollback)
        to_version: Target version (for rollback)
    """
    # Build record with only non-None values
    record: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "operation": operation,
        "user": getpass.getuser(),
        "host": host,
        "app_name": app_name,
    }

    # Add optional fields if provided
    if version is not None:
        record["version"] = version
    if git_commit is not None:
        record["git_commit"] = git_commit
    if from_version is not None:
        record["from_version"] = from_version
    if to_version is not None:
        record["to_version"] = to_version

    # Append to centralized audit log outside app directory
    log_file = f"/opt/fujin/.audit/{app_name}.log"
    json_line = json.dumps(record)

    # Ensure audit directory exists
    connection.run("sudo mkdir -p /opt/fujin/.audit")
    connection.run(f"echo {json.dumps(json_line)} | sudo tee -a {log_file} >/dev/null")


def read_logs(
    connection: SSH2Connection,
    app_name: str,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """
    Read audit logs from the server.

    Args:
        connection: SSH connection to the server
        app_name: Application name
        limit: Maximum number of records to return (most recent first)

    Returns:
        List of audit log records
    """
    log_file = f"/opt/fujin/.audit/{app_name}.log"

    # Check if log file exists
    stdout, success = connection.run(f"test -f {log_file}", warn=True, hide=True)
    if not success:
        return []

    # Fetch last N lines efficiently using tail
    if limit:
        stdout, _ = connection.run(f"tail -n {limit} {log_file}", hide=True)
    else:
        stdout, _ = connection.run(f"cat {log_file}", hide=True)

    # Parse JSONL
    records = []
    for line in stdout.strip().split("\n"):
        line = line.strip()
        if line:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # Skip malformed lines

    # Return most recent first
    records.reverse()

    return records
