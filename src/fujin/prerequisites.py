"""Declarative host prerequisites (``[prerequisites]`` / #97).

Two kinds of host setup are supported:

- ``packages``: apt package names. They are installed by ``server bootstrap``
  (or ``server upgrade``) through the deploy user's scoped sudoers rule, and
  the same validated list is rendered into that rule's ``packages`` group.
- ``commands``: idempotent *unprivileged* setup steps, run as the deploy user
  without sudo. ``check`` runs first; ``run`` is skipped when the check exits
  0. Tools install into ``{shared_dir}/bin``, which is prepended to hook and
  service ``PATH`` so the tool is visible to both.

The module also owns the root-owned marker at
``/etc/fujin/<app>/prerequisites.json``. ``server bootstrap`` writes it after
applying prerequisites; ``fujin doctor`` reads it (read-only, no sudo) and
reports drift. ``deploy`` never touches any of this.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import shlex
from collections.abc import Iterable
from typing import Any

from fujin.config import Config, HostConfig, PrerequisiteCommand
from fujin.formatting import safe_format

#: Packages fujin itself needs on every host. They are installed by bootstrap
#: and upgrade but, unlike configured prerequisites, they do not gate doctor.
CORE_PACKAGES: tuple[str, ...] = ("sqlite3", "curl", "rsync")

#: systemd's default service ``PATH``; configured tools are prepended to it.
SYSTEM_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

MARKER_DIR = "/etc/fujin"
MARKER_NAME = "prerequisites.json"
MARKER_MODE = 0o644

_ENV_PATH_RE = re.compile(r"^\s*Environment\s*=\s*\"?PATH=", re.IGNORECASE)


# =============================================================================
# Configured values
# =============================================================================


def configured_packages(config: Config, host: HostConfig) -> list[str]:
    """The host's effective ``prerequisites.packages`` (replace semantics)."""
    prereqs = config.effective_prerequisites(host)
    return list(prereqs.packages) if prereqs else []


def configured_commands(config: Config, host: HostConfig) -> list[PrerequisiteCommand]:
    prereqs = config.effective_prerequisites(host)
    return list(prereqs.commands) if prereqs else []


def system_packages(config: Config, host: HostConfig) -> list[str]:
    """Core packages plus configured ones, in stable, duplicate-free order."""
    return _dedupe((*CORE_PACKAGES, *configured_packages(config, host)))


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def command_context(config: Config, host: HostConfig) -> dict[str, object]:
    """Template variables available to prerequisite ``check``/``run``."""
    return {
        "app_name": config.app_name,
        "app_user": config.app_user,
        "version": config.version,
        "app_dir": config.app_dir,
        "shared_dir": config.shared_dir,
        "user": host.user,
    }


def render_command(
    template: str, config: Config, host: HostConfig
) -> tuple[str, set[str]]:
    return safe_format(template, **command_context(config, host))


# =============================================================================
# Marker / fingerprint
# =============================================================================


def marker_path(app_name: str) -> str:
    return f"{MARKER_DIR}/{app_name}/{MARKER_NAME}"


def _fingerprint_data(config: Config, host: HostConfig) -> dict[str, Any]:
    """Identity of the *configured* prerequisites for one host."""
    return {
        "packages": configured_packages(config, host),
        "commands": [
            {"id": command.id, "check": command.check, "run": command.run}
            for command in configured_commands(config, host)
        ],
        "shared_dir": config.shared_dir,
    }


def fingerprint(config: Config, host: HostConfig) -> str:
    payload = json.dumps(
        _fingerprint_data(config, host),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def marker_content(config: Config, host: HostConfig) -> str:
    data = _fingerprint_data(config, host)
    data["hash"] = fingerprint(config, host)
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def read_marker(conn: Any, app_name: str) -> dict[str, Any] | None:
    """Read the root-owned marker without sudo (mode 0644)."""
    out, ok = conn.run(
        f"cat {shlex.quote(marker_path(app_name))}", warn=True, hide=True
    )
    if not ok:
        return None
    try:
        value = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def write_marker(conn: Any, config: Config, host: HostConfig) -> None:
    """Write the marker root-owned and world-readable via the operator's sudo."""
    path = marker_path(config.app_name)
    directory = shlex.quote(f"{MARKER_DIR}/{config.app_name}")
    payload = marker_content(config, host).encode("utf-8")
    encoded = base64.b64encode(payload).decode("ascii")
    script = (
        f"sudo mkdir -p {directory}"
        f" && printf '%s' {shlex.quote(encoded)} | base64 -d"
        f" | sudo tee {shlex.quote(path)} >/dev/null"
        f" && sudo chmod {MARKER_MODE:04o} {shlex.quote(path)}"
    )
    _, ok = conn.run(script, warn=True, hide=True)
    if not ok:
        raise RuntimeError(f"Failed to write prerequisites marker {path}")


# =============================================================================
# Applying prerequisites (server bootstrap / upgrade)
# =============================================================================


def ensure_shared_bin(conn: Any, config: Config) -> None:
    """Create ``{shared_dir}/bin`` for tools installed by commands."""
    path = shlex.quote(config.shared_bin)
    _, ok = conn.run(f"mkdir -p {path}", warn=True, hide=True)
    if not ok:
        raise RuntimeError(f"Failed to create {config.shared_bin}")


def apply(conn: Any, config: Config, host: HostConfig, output: Any = None) -> None:
    """Run configured prerequisite commands, check-first and idempotently.

    Commands run as the connecting (deploy) user with no sudo. A failing
    ``run`` aborts: a half-provisioned host must not be recorded as applied.
    """
    commands = configured_commands(config, host)
    if not commands:
        return
    ensure_shared_bin(conn, config)
    for command in commands:
        check, check_unresolved = render_command(command.check, config, host)
        run, run_unresolved = render_command(command.run, config, host)
        unresolved = check_unresolved | run_unresolved
        if unresolved:
            raise RuntimeError(
                f"Prerequisite command '{command.id}' references unknown"
                f" variables: {', '.join(sorted(unresolved))}"
            )
        path_prefix = f"export PATH={shlex.quote(config.shared_bin)}:$PATH; "
        out, ok = conn.run(path_prefix + check, warn=True, hide=True)
        if ok:
            if output is not None:
                output.info(f"  [prereq] {command.id}: check passed; skipped")
            continue
        if output is not None:
            output.info(f"  [prereq] {command.id}: installing...")
        script = f"cd {shlex.quote(config.app_dir)} && {path_prefix}{run}"
        out, ok = conn.run(script, warn=True)
        if not ok:
            detail = out.strip().splitlines()[-1] if out.strip() else "no output"
            raise RuntimeError(f"Prerequisite command '{command.id}' failed: {detail}")
        if output is not None:
            output.success(f"  [prereq] {command.id}: installed")


# =============================================================================
# Service PATH
# =============================================================================


def service_path(shared_bin: str) -> str:
    return f"{shared_bin}:{SYSTEM_PATH}"


def ensure_service_path(content: str, shared_bin: str) -> str:
    """Prepend *shared_bin* to a service unit's ``PATH``.

    Rewrites an existing ``Environment=PATH=...`` line (quoted or not) in
    place; otherwise inserts one just below ``[Service]``. Non-service units
    are returned unchanged.
    """
    line = f"Environment=PATH={service_path(shared_bin)}"
    trailing = "\n" if content.endswith("\n") else ""
    lines = content.splitlines()

    for index, existing in enumerate(lines):
        if _ENV_PATH_RE.match(existing):
            lines[index] = line
            return "\n".join(lines) + trailing

    for index, existing in enumerate(lines):
        if existing.strip().lower() == "[service]":
            lines.insert(index + 1, line)
            return "\n".join(lines) + trailing
    return content
