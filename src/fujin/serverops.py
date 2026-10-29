"""Client-driven server-side operations for the release-tree store.

Everything here mutates the server through :class:`SSH2Connection.run` /
:meth:`~SSH2Connection.put`, reusing the sudo/pty conventions of
``commands/server.py``. Pure
decision-making lives in ``store.py``; this module is the I/O port layer.
"""

from __future__ import annotations

import base64
import configparser
import json
import re
import secrets
import logging
import shlex
import time
import msgspec
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import cappa

from fujin import manifest as m
from fujin.audit import write_command as audit_write_command
from fujin.errors import ConnectionError, ImproperlyConfiguredError
from fujin.formatting import safe_format
from fujin.redaction import redact
from fujin.store import (
    State,
    convergence,
    desired_instances,
    gc_plan,
    instance_sort_key,
    parse_state,
    serialize_state,
    unit_entry_name,
)

logger = logging.getLogger(__name__)

UV_PYTHON_INSTALL_DIR = "/opt/fujin/.python"
#: Holder TTL must outlive the longest deploy phase (the rsync transfer alone
#: allows up to 3600s); renewal is done implicitly by releasing and re-taking.
LOCK_TTL = 7200
#: Holder acquisition is polled on the server: 40 x 50ms = 2s worst case,
#: instead of an unconditional 500ms sleep on every deploy.
LOCK_POLL_INTERVAL = 0.05
LOCK_POLL_ATTEMPTS = 40
LOCK_PATH = "store/lock"
LEGACY_LOCK_PATH = ".deploy_lock"
STATE_PATH = "store/state.json"
MANIFEST_PATH = "manifest.json"
ENV_PATH = ".env"
SYSTEMD_DIR = "/etc/systemd/system"

_PYTHON_ROOT = "venv"
_BINARY_ROOT = "bin"

#: Marker created immediately before the clone-path wheel install; the venv
#: permission passes only touch entries ``-newer`` than it, so hardlinked
#: live-release inodes stay untouched. Removed by the permission pass.
CLONE_MARKER_PATH = ".fujin-install-marker"

#: End-of-section markers for the fused ``install_units`` script: the scans
#: are printed first (diagnostics), the mutating steps follow.
INSTALL_INSTALLED_END = "FUJIN-INSTALLED-END"
INSTALL_EMPTY_END = "FUJIN-EMPTY-END"
#: ``FUJIN-FAIL:<step>[:<target>]`` names the step that aborted a fused script.
INSTALL_FAIL = "FUJIN-FAIL:"

#: End-of-section markers for the fused ``template_instance_plan`` query.
PLAN_ENABLED_END = "FUJIN-ENABLED-END"
PLAN_LINKS_END = "FUJIN-LINKS-END"

#: Section markers for the fused readiness probe (one round-trip per tick).
READINESS_STATUS_END = "FUJIN-STATUS-END"
READINESS_SOCKET_BEGIN = "FUJIN-SOCKET"
READINESS_SOCKET_END = "FUJIN-SOCKET-END"

#: Fallback readiness window: a unit must reach ``active`` within this many
#: seconds of the restart, regardless of any probe configuration.
READINESS_TIMEOUT = 30.0
READINESS_INTERVAL = 0.5

#: Retry interval of the optional health-check probe, in seconds.
HEALTH_CHECK_INTERVAL = 1.0


class ServerOpsError(RuntimeError):
    """A server-side operation failed."""


def q(*parts: str) -> str:
    return " ".join(shlex.quote(str(p)) for p in parts)


def unit_rel_name(path: str) -> str:
    """Strip the ``systemd/`` prefix from a manifest entry path."""
    return path.split("/", 1)[1] if path.startswith("systemd/") else path


def expand_patterns(unit_names: Iterable[str]) -> list[str]:
    """Expand template units to ``@*.service`` glob patterns for systemctl."""
    return sorted(
        unit_rel_name(unit).replace("@.service", "@*.service")
        if unit.endswith("@.service")
        else unit_rel_name(unit)
        for unit in unit_names
        if unit
    )


def is_oneshot(service_content: str) -> bool:
    """Type=oneshot detection from a service unit file body."""
    parser = configparser.RawConfigParser(strict=False)
    parser.read_string(service_content)
    if not parser.has_option("Service", "Type"):
        return False
    return parser.get("Service", "Type").strip().lower() == "oneshot"


def service_unit_names(manifest: m.Manifest) -> list[str]:
    return sorted(
        unit_rel_name(path)
        for path, entry in manifest.entries.items()
        if entry.kind is m.EntryKind.UNIT and unit_rel_name(path).endswith(".service")
    )


def socket_timer_unit_names(manifest: m.Manifest) -> set[str]:
    return {
        unit_rel_name(path)
        for path, entry in manifest.entries.items()
        if entry.kind is m.EntryKind.UNIT
        and unit_rel_name(path).endswith((".socket", ".timer"))
    }


def read_remote_file(conn: Any, path: str, *, fatal: str | None = None) -> str | None:
    """Read a small remote file; ``fatal`` makes failure abort with that
    message instead of returning None.

    Uses SCP (not ``cat``): release manifests can be hundreds of KB and big
    exec-channel reads can trickle forever (see SSH2Connection.get).
    """
    import os
    import tempfile

    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp_path = tmp.name
    try:
        try:
            conn.get(path, tmp_path)
        except (Exception, cappa.Exit):
            # ``cappa.Exit`` derives from ``SystemExit``, not ``Exception``:
            # the transport raises ``UploadError`` for a missing/unreadable
            # file (a first deploy has no live manifest), which must mean
            # "absent" here, not abort the deploy.
            if fatal:
                raise cappa.Exit(fatal, code=1) from None
            return None
        with open(tmp_path, "rb") as fh:
            return fh.read().decode("utf-8", errors="replace")
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            # conn.get unlinks the file itself when the download fails.
            pass


def expand_dropins(
    unit_contents: Iterable[tuple[str, str]],
) -> tuple[dict[str, str], dict[str, dict[str, str]], set[str]]:
    """Partition systemd tree entries into unit files / per-unit dropins /
    oneshots.

    ``unit_contents`` yields ``(unit_rel_name, content)`` pairs of a ``systemd``
    tree. ``dropins`` maps unit names to ``{name.conf: content}`` with
    ``common.d`` expanded per service unit (``install_units`` contract).
    """
    unit_files: dict[str, str] = {}
    service_dropins: dict[str, dict[str, str]] = {}
    common_dropins: dict[str, str] = {}

    for rel, content in unit_contents:
        if rel.startswith("common.d/"):
            common_dropins[rel.split("/", 1)[1]] = content
        elif rel.endswith(".conf") and ".d/" in rel:
            continue  # handled below (flat dropin dir)
        else:
            unit_files[rel] = content

    for unit in list(unit_files):
        if unit.endswith(".service"):
            for name, content in common_dropins.items():
                service_dropins.setdefault(unit, {}).setdefault(name, content)

    # Flat dropin dirs: any unit type ("svc.d/", "svc.socket.d/",
    # "svc.timer.d/") -> dropins keyed by the full unit name.
    for rel, content in unit_contents:
        if rel.endswith(".conf") and ".d/" in rel and not rel.startswith("common.d/"):
            unit_dir, _, dropin_name = rel.rpartition("/")
            unit = unit_dir.removesuffix(".d")
            service_dropins.setdefault(unit, {})[dropin_name] = content

    oneshots = {
        unit
        for unit, content in unit_files.items()
        if unit.endswith(".service") and is_oneshot(content)
    }
    return unit_files, service_dropins, oneshots


def collect_release_units(
    conn: Any, release_path: str, manifest: m.Manifest
) -> tuple[dict[str, str], dict[str, dict[str, str]], set[str], list[str]]:
    """Unit files + dropins + oneshots of a committed release tree.

    Reads the release tree's ``systemd/`` files. Returns
    ``(unit_files, dropins, oneshots, failed_rels)`` -- the caller decides how
    to treat unreadable units.
    """
    systemd_path = f"{release_path}/systemd"
    unit_contents: list[tuple[str, str]] = []
    failed: list[str] = []

    unit_paths = sorted(
        path
        for path, entry in manifest.entries.items()
        if entry.kind is m.EntryKind.UNIT
    )
    for path in unit_paths:
        rel = unit_rel_name(path)
        content = read_remote_file(conn, f"{systemd_path}/{rel}")
        if content is None:
            logger.warning("Cannot read unit file '%s' from the release tree", rel)
            failed.append(rel)
            continue
        unit_contents.append((rel, content))

    unit_files, dropins, oneshots = expand_dropins(unit_contents)
    return unit_files, dropins, oneshots, failed


def list_releases(conn: Any, app_dir: str) -> list[str]:
    """Digests under releases/, newest (mtime) first; staging dirs excluded."""
    out, _ok = conn.run(
        f"cd {shlex.quote(app_dir)} && ls -1t releases/ 2>/dev/null || true",
        warn=True,
        hide=True,
    )
    return [
        line.strip()
        for line in out.splitlines()
        if line.strip() and not line.strip().startswith(".staging-")
    ]


def list_staging_dirs(conn: Any, app_dir: str) -> list[str]:
    """Orphaned ``releases/.staging-*`` directories, as absolute paths."""
    out, _ok = conn.run(
        f"find {shlex.quote(f'{app_dir}/releases')} -mindepth 1 -maxdepth 1"
        " -type d -name '.staging-*' -print 2>/dev/null | sort",
        warn=True,
        hide=True,
    )
    return [line.strip() for line in out.splitlines() if line.strip()]


def _remove_releases(conn: Any, app_dir: str, digests: Sequence[str]) -> bool:
    """Remove all *digests* in ONE ``rm -rf``; returns whether it succeeded."""
    targets = " ".join(
        shlex.quote(f"{app_dir}/releases/{digest}") for digest in digests
    )
    _, ok = conn.run(f"rm -rf {targets}", warn=True, hide=True)
    return ok


def collect_garbage(
    conn: Any,
    app_dir: str,
    state: State,
    releases: list[str],
    keep_budget: int | None,
    output: Any,
) -> tuple[str, ...]:
    """Delete releases outside ``store.gc_plan``'s keep-set; returns digest list."""
    kill = gc_plan(state.current, state.previous, releases, keep_budget)
    if kill:
        if _remove_releases(conn, app_dir, kill):
            output.info(f"Garbage-collected {len(kill)} old release(s).")
        else:
            output.warning(f"Failed to remove {len(kill)} old release(s).")
    return kill


#: Markers of the fused ``finalize_deploy`` script: one per sub-step so a
#: failure can be attributed without a second round-trip.
FINALIZE_STATE_FAILED = "FUJIN-FAIL:state"
FINALIZE_AUDIT_WARNED = "FUJIN-WARN:audit"
FINALIZE_RELEASES_BEGIN = "FUJIN-RELEASES"
FINALIZE_RELEASES_END = "FUJIN-RELEASES-END"


def finalize_deploy(
    conn: Any,
    app_dir: str,
    state: State | None,
    *,
    keep_budget: int | None,
    audit_line: str | None = None,
    app_name: str | None = None,
    persist_state: bool = True,
    output: Any = None,
) -> tuple[list[str], tuple[str, ...]]:
    """Post-activation bookkeeping: state, release list, GC and the audit line.

    Fuses what used to be 5+ sequential calls into two:

    1. one script that writes ``state.json`` atomically, lists ``releases/``
       and appends *audit_line* (``mkdir`` + ``tee`` fused). The audit append
       stays best-effort, like :func:`fujin.audit.log_operation`.
    2. a single ``rm -rf`` for every digest outside ``store.gc_plan``'s
       keep-set, skipped entirely when nothing is collectable.

    ``state=None`` (auto-rollback with no recorded state) skips both the
    state write and GC, matching the pre-fusion behaviour; so does a state
    with ``current=None`` (no live release anchors the keep-set, so every
    release would look collectable). A no-op deploy still passes its live
    state for the GC decision but sets ``persist_state=False``: nothing
    changed, so nothing is rewritten.

    Returns ``(releases, removed)``; raises :class:`ServerOpsError` naming the
    failed sub-step when the state write (or the script itself) fails.
    """
    state_path = f"{app_dir}/{STATE_PATH}"
    script: list[str] = []
    if state is not None and persist_state:
        tmp_q = shlex.quote(f"{state_path}.tmp")
        script.append(
            f"printf '%s' {_b64(serialize_state(state))}"
            f" | base64 -d > {tmp_q} && mv -f {tmp_q} {shlex.quote(state_path)}"
            f" || {{ echo '{FINALIZE_STATE_FAILED}'; exit 1; }}"
        )
    script.append(f"echo '{FINALIZE_RELEASES_BEGIN}'")
    script.append(f"cd {shlex.quote(app_dir)} && ls -1t releases/ 2>/dev/null || true")
    script.append(f"echo '{FINALIZE_RELEASES_END}'")
    if audit_line is not None:
        if app_name is None:
            raise ValueError("app_name is required to append the audit entry")
        script.append(
            f"{{ {audit_write_command(app_name, audit_line)}; }}"
            f" || echo '{FINALIZE_AUDIT_WARNED}'"
        )

    out, ok = conn.run("\n".join(script), warn=True, hide=True)
    if FINALIZE_STATE_FAILED in out:
        raise ServerOpsError(
            f"Failed to write {state_path} on server (finalize step: state)"
        )
    if not ok:
        raise ServerOpsError(
            f"Deploy finalize failed on the server (finalize step: bookkeeping):\n"
            f"{out.strip()}"
        )

    releases: list[str] = []
    listing = out.split(f"{FINALIZE_RELEASES_BEGIN}\n", 1)
    if len(listing) == 2:
        for line in listing[1].split(f"{FINALIZE_RELEASES_END}", 1)[0].splitlines():
            digest = line.strip()
            if digest and not digest.startswith(".staging-"):
                releases.append(digest)

    if FINALIZE_AUDIT_WARNED in out:
        message = f"Failed to append the audit entry for {app_name}"
        if output is not None:
            output.warning(message)
        else:
            logger.warning(message)

    if state is None or state.current is None:
        # No recorded state, or no live release to anchor the keep-set:
        # with ``current`` missing every release looks collectable, so
        # skipping GC is the only safe answer.
        return releases, ()

    removed = gc_plan(state.current, state.previous, releases, keep_budget)
    if removed and not _remove_releases(conn, app_dir, removed):
        message = (
            f"Failed to remove {len(removed)} old release(s)"
            " (finalize step: garbage collection)"
        )
        if output is not None:
            output.warning(message)
        else:
            logger.warning(message)
    elif removed and output is not None:
        output.info(f"Garbage-collected {len(removed)} old release(s).")
    return releases, removed


def _b64(content: bytes | str) -> str:
    if isinstance(content, str):
        content = content.encode("utf-8")
    return base64.b64encode(content).decode("ascii")


def _python_script(body: str, marker: str = "FUJINSCRIPT") -> str:
    """Wrap an inline python program for execution over ``conn.run``."""
    return f"python3 - <<'{marker}'\n{body}\n{marker}"


def _sudo_write_command(
    path: str, content: bytes | str, *, chown: str | None = None
) -> str:
    """The base64-stream + ``mv`` command that writes a root-owned path.

    Kept as one composable string so fused scripts (``install_units``) keep
    the exact per-file ``sudo tee`` / ``sudo mv`` shapes the scoped sudoers
    rule matches.
    """
    path_q = shlex.quote(path)
    tmp_q = shlex.quote(f"{path}.fujin-new")
    cmd = (
        f"echo {_b64(content)} | base64 -d | sudo tee {tmp_q} >/dev/null"
        f" && sudo mv {tmp_q} {path_q}"
    )
    if chown is not None:
        cmd += f" && sudo chown {chown} {path_q}"
    return cmd


def _sudo_write(
    conn: Any, path: str, content: bytes | str, *, chown: str | None = None
) -> None:
    """Write file content to a root-owned path via base64 stream + mv."""
    cmd = _sudo_write_command(path, content, chown=chown)
    _, ok = conn.run(cmd, warn=True, hide=True)
    if not ok:
        raise ServerOpsError(f"Failed to write {path} on server")


# =============================================================================
# Layout (bootstrap)
# =============================================================================


def ensure_layout(conn: Any, app_dir: str, app_user: str, deploy_user: str) -> None:
    """Create the release-tree layout, idempotently.

    ``{app_dir}``, ``releases/``, ``store/`` are setgid ``2755``
    (``{deploy_user}:{app_user}``); ``shared/`` is group-writable ``2775``;
    ``store/lock`` is 0664. Directory modes are setgid so files created under
    them inherit the ``app_user`` group.
    """
    app_q = shlex.quote(app_dir)
    script = (
        f"sudo mkdir -p {app_q}/releases {app_q}/store {app_q}/shared"
        f" && sudo chown {deploy_user}:{app_user}"
        f" {app_q} {app_q}/releases {app_q}/store {app_q}/shared"
        f" && sudo chmod 2755 {app_q} {app_q}/releases {app_q}/store"
        f" && sudo chmod 2775 {app_q}/shared"
        f" && sudo touch {app_q}/{LOCK_PATH}"
        f" && sudo chown {deploy_user}:{app_user} {app_q}/{LOCK_PATH}"
        f" && sudo chmod 0664 {app_q}/{LOCK_PATH}"
    )
    _, ok = conn.run(script, warn=True, hide=True)
    if not ok:
        raise ServerOpsError(f"Failed to set up release-tree layout in {app_dir}")


# =============================================================================
# App user
# =============================================================================


def ensure_app_user(conn: Any, app_user: str) -> None:
    """Create the app system user/group idempotently (no home, nologin).

    One round-trip, three fused steps: ensure the group, create the user only
    when it is missing, then prove the uid resolves. The group guard is not
    short-circuited by the user probe: a pre-existing user whose group is
    missing would otherwise make the follow-up ``chown deploy:<app_user>``
    fail. ``getent group`` keeps the fast path sudo-free when both exist.
    Raises when the user/group cannot be created (mirrors the retired
    installer PHASE 1 and ``server bootstrap``'s user creation).
    """
    user_q = shlex.quote(app_user)
    cmd = (
        f"{{ getent group {user_q} >/dev/null 2>&1 || sudo groupadd -f {user_q}; }}"
        " && "
        f"{{ id -u {user_q} >/dev/null 2>&1 || "
        "sudo useradd --system --no-create-home --shell /usr/sbin/nologin "
        f"-g {user_q} {user_q}; }}"
        f" && id -u {user_q}"
    )
    _, ok = conn.run(cmd, warn=True, hide=True)
    if not ok:
        raise ServerOpsError(f"Failed to create app user {app_user}")


# =============================================================================
# Deploy lock
# =============================================================================


def acquire_lock(conn: Any, app_dir: str, *, ttl: int = LOCK_TTL) -> str:
    """Take the deploy lock, failing loudly if another deployment holds it.

    Two belts:

    1. ``flock`` on ``store/lock`` held by a detached holder process for
       ``ttl`` seconds (preferred, works across shells). The holder takes
       the flock FIRST (``flock -n 9 || exit 1``) and writes
       ``<pid>-<token>`` to ``store/lock.holder`` only afterwards, so a
       holder that loses the race exits without touching the marker of
       whoever holds the lock. Every acquisition attempt uses a fresh random
       token and the parent accepts a probe line only when it names that
       token AND the flock is still taken: a blocked or foreign holder can
       never be mistaken for our success. A stale marker (dead pid, flock
       free) is cleaned up and retried once.
    2. The legacy noclobber ``.deploy_lock`` file (``set -C``) only when
       ``flock`` is unavailable on the host.

    A foreign *live* holder never causes a belt switch: we raise rather than
    grab the noclobber file the flock holder knows nothing about.

    Returns an opaque lock token to pass to :func:`release_lock`.
    Raises :class:`ServerOpsError` when the lock is held elsewhere.
    """
    app_q = shlex.quote(app_dir)
    lock_q = shlex.quote(f"{app_dir}/{LOCK_PATH}")
    holder_q = shlex.quote(f"{app_dir}/{LOCK_PATH}.holder")

    _, has_flock = conn.run("command -v flock", warn=True, hide=True)

    if has_flock:
        for _ in range(2):  # one retry after clearing a stale holder
            token = secrets.token_hex(8)
            # Fully detached from the SSH exec session: setsid + nohup +
            # </dev/null. A partially detached background child would keep
            # the SSH channel from reaching EOF and make conn.run hang.
            # The flock comes BEFORE the marker write: a blocked holder must
            # not clobber the marker of the process that owns the lock.
            holder_cmd = (
                f'setsid nohup bash -c "exec 9>>{lock_q}; '
                f"flock -n 9 || exit 1; "
                f"echo \\$\\$-{token} > {holder_q}; "
                f'sleep {ttl}" >/dev/null 2>&1 & echo $!'
            )
            out, ok = conn.run(holder_cmd, warn=True, hide=True)
            holder_pid = out.strip() if ok else ""

            # Poll for OUR token instead of sleeping a fixed 500ms: the
            # marker appears only after the flock is owned, and it is only
            # reported as OURS while ``flock -n`` still fails and the marker
            # pid is alive (a dead holder can leave a stale marker while a
            # foreign process holds the flock). After the deadline the flock
            # is judged in the state it is in *then*: held (BUSY, with
            # whatever marker exists, ``?`` when none), free with a live
            # marker (ALIVE) or free with a dead one (DEAD).
            probe, _ = conn.run(
                f"tok={token}; hf={holder_q}; lk={lock_q}; i=0; "
                f'while [ "$i" -lt {LOCK_POLL_ATTEMPTS} ]; do '
                f'h=$(cat "$hf" 2>/dev/null || true); '
                f'case "$h" in *-"$tok") '
                f'if ! flock -n "$lk" true 2>/dev/null; '
                f'then echo "OURS:$h"; exit 0; fi ;; esac; '
                f"sleep {LOCK_POLL_INTERVAL}; i=$((i + 1)); done; "
                f'h=$(cat "$hf" 2>/dev/null || true); '
                f'case "$h" in *-"$tok") hp=${{h%%-*}}; '
                f'if [ -n "$hp" ] && kill -0 "$hp" 2>/dev/null && '
                f'! flock -n "$lk" true 2>/dev/null; '
                f'then echo "OURS:$h"; exit 0; fi ;; esac; '
                f'if ! flock -n "$lk" true 2>/dev/null; '
                f'then echo "BUSY:${{h:-?}}"; '
                f'elif [ -n "$h" ]; then hp=${{h%%-*}}; '
                f'if [ -n "$hp" ] && kill -0 "$hp" 2>/dev/null; '
                f'then echo "ALIVE:$h"; else echo "DEAD:$h"; fi; '
                f'else echo "FREE:"; fi',
                warn=True,
                hide=True,
            )
            first = (probe.strip().splitlines() or [""])[0]
            status, _, marker = first.partition(":")
            marker = "" if marker.strip() == "?" else marker.strip()
            file_pid, _, file_token = marker.partition("-")

            # Only our own token, confirmed while the flock is still taken,
            # counts as ownership.
            if status == "OURS" and file_pid and file_token == token:
                return f"flock:{file_pid}"

            if status == "BUSY":
                # The flock is owned and no marker names our token: the
                # holder we spawned lost the race and wrote nothing.
                by = f" by pid {file_pid}" if file_pid else ""
                raise ServerOpsError(
                    "Another deployment is in progress on the server\n"
                    f"({app_dir}/{LOCK_PATH} is flock'ed{by})."
                    " Wait for it to finish."
                )

            if status == "ALIVE":
                # Marker of a live process while the flock is free: it may
                # be about to acquire, so never steal from it and never
                # switch belts.
                raise ServerOpsError(
                    "Another deployment is in progress on the server\n"
                    f"(flock holder pid {file_pid}). Wait for it to finish."
                )

            # DEAD: stale marker (dead pid, flock free) -> clean and retry.
            # FREE: our holder never got the flock (unsupported filesystem,
            # ...) -> the noclobber belt is safe.
            conn.run(
                f"kill {shlex.quote(holder_pid)} 2>/dev/null; "
                f"kill {shlex.quote(file_pid)} 2>/dev/null; true",
                warn=True,
                hide=True,
            )
            # Compare-and-delete: never remove a marker another deployer
            # wrote in the meantime.
            conn.run(
                f"h=$(cat {holder_q} 2>/dev/null || true); "
                f'[ "$h" = {shlex.quote(marker)} ] && rm -f {holder_q}; true',
                warn=True,
                hide=True,
            )
            if status != "DEAD":
                break
        else:
            raise ServerOpsError(
                "Could not acquire the flock deploy lock (stale holder persisted).\n"
                f"Remove {app_dir}/{LOCK_PATH}.holder manually if no deploy is running."
            )

    # Fallback: noclobber file, as in commands/deploy.py.
    lock_file = f"{app_dir}/{LEGACY_LOCK_PATH}"
    lock_q = shlex.quote(lock_file)
    _, acquired = conn.run(
        f"mkdir -p {app_q} && (set -C; echo $$ > {lock_q}) 2>/dev/null",
        warn=True,
        hide=True,
    )
    if not acquired:
        raise ServerOpsError(
            "Another deployment is in progress.\n"
            "If you are sure no deploy is running, remove the lock manually:\n"
            f"  ssh fujin@host rm {lock_file}"
        )
    return f"file:{lock_file}"


def release_lock(conn: Any, app_dir: str, lock: str) -> None:
    """Release a lock acquired by :func:`acquire_lock`.

    Kills the holder's whole process GROUP: the holder bash is a setsid
    group leader and its ``flock`` child inherits nothing else — killing
    only the bash would orphan the flock holding the lock forever.
    """
    if lock.startswith("flock:"):
        pid = lock.removeprefix("flock:")
        pid_q = shlex.quote(pid)
        holder_q = shlex.quote(f"{app_dir}/{LOCK_PATH}.holder")
        # One round trip: kill the group + holder, then drop the marker. The
        # marker removal is unconditional (``;``, not ``&&``) so a stale
        # holder file never survives a dead pid.
        conn.run(
            f"kill -- -{pid_q} 2>/dev/null; kill {pid_q} 2>/dev/null;"
            f" rm -f {holder_q}; true",
            warn=True,
            hide=True,
        )
    else:
        path = lock.removeprefix("file:")
        conn.run(f"rm -f {shlex.quote(path)}", warn=True, hide=True)


def parse_holder_pid(output: str) -> str | None:
    """Parse the holder pid from an acquire_lock probe line, or None if free.

    Probe lines are ``<STATUS>:<pid>-<token>``; ``FREE:`` and ``BUSY:?``
    record no holder pid.
    """
    _, _, marker = output.strip().partition(":")
    marker = "" if marker.strip() == "?" else marker.strip()
    pid, _, _ = marker.partition("-")
    return pid or None


class LockProbe(msgspec.Struct, frozen=True):
    """Read-only view of the deploy lock (``fujin doctor``).

    ``state`` is one of ``free`` (nobody holds it), ``live`` (holder pid
    alive), ``held`` (flock held but no holder pid recorded), ``stale``
    (holder file with a dead pid) or ``stale-legacy`` (noclobber lock file
    with a dead pid).
    """

    state: str
    pid: str | None = None


def probe_lock(conn: Any, app_dir: str) -> LockProbe:
    """Probe the deploy lock without taking it (strictly read-only).

    The flock itself is tested with ``flock -n`` (does not hold beyond the
    probe). A recorded holder pid is only reported as ``live`` when
    ``kill -0`` confirms it.
    """
    lock_path = f"{app_dir}/{LOCK_PATH}"
    holder = f"{lock_path}.holder"
    legacy = f"{app_dir}/{LEGACY_LOCK_PATH}"
    cmd = (
        f"lock={shlex.quote(lock_path)}; holder={shlex.quote(holder)}; "
        f"legacy={shlex.quote(legacy)}; state=free; pid=; "
        f'if [ -f "$holder" ]; then pid=$(cut -d- -f1 "$holder" 2>/dev/null); fi; '
        f'if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then state=live; '
        f'elif [ -f "$legacy" ]; then pid=$(cat "$legacy" 2>/dev/null); '
        f'if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then state=live; '
        f"else state=stale-legacy; fi; "
        f'elif [ -n "$pid" ]; then state=stale; '
        f'elif [ -e "$lock" ] && command -v flock >/dev/null 2>&1 '
        f'&& ! flock -n "$lock" true 2>/dev/null; then state=held; pid=; '
        f'fi; echo "$state:${{pid:-}}"'
    )
    out, _ok = conn.run(cmd, warn=True, hide=True)
    line = out.strip().rsplit("\n", 1)[-1] if out.strip() else ""
    state, _, pid = line.partition(":")
    if not state:
        return LockProbe(state="free")
    return LockProbe(state=state, pid=pid or None)


# =============================================================================
# Code install (into staging; deploy step 4)
# =============================================================================


def _fail_marker(step: str, target: str | None = None) -> str:
    """Shell suffix that aborts a fused script with a ``FUJIN-FAIL`` marker.

    Fused scripts lose the per-step exit status that separate ``conn.run``
    calls had; the marker restores precise failure attribution (which file,
    which step) without extra round-trips.
    """
    marker = f"{INSTALL_FAIL}{step}"
    if target is not None:
        marker += f":{target}"
    return f"|| {{ printf '%s\\n' {shlex.quote(marker)}; exit 1; }}"


def _failed_step(output: str) -> str | None:
    """Value after the last ``FUJIN-FAIL:`` marker in *output* (or None)."""
    failed = None
    for line in output.splitlines():
        text = line.strip()
        if text.startswith(INSTALL_FAIL):
            failed = text[len(INSTALL_FAIL) :]
    return failed


def prepare_staging(
    conn: Any,
    staging_dir: str,
    installation_mode: str,
    python_version: str | None,
    requirements: str | None,
    dist_tree_path: str,
    *,
    app_name: str,
    app_user: str,
    clone_from: str | None = None,
) -> None:
    """Install the app code and apply the deploy permission model to staging.

    Never touches a live venv's *content*. The steps and the number of
    round-trips:

    - ``python-package``, full install: ``uv venv`` (1), ``uv pip install``
      (1), then the fused permission pass (1) below.
    - ``python-package``, ``clone_from`` set: ``cp -al`` of the live
      release's ``venv/`` (1), ``uv pip install --no-deps
      --compile-bytecode`` of the new dist (1), then the fused permission
      pass (1). Hardlinked dependency files are never read, rewritten or
      byte-compiled; only the app distribution is installed into the clone.
      ``--compile-bytecode`` compiles exactly that newly installed package
      (writing fresh ``__pycache__`` inodes, never through a hardlink). The
      permission pass only touches the files this install created: a marker
      file is created immediately before ``uv pip install`` and the venv
      passes select ``find <venv> -newer <marker>``, so the live release's
      hardlinked inodes keep their modes and ctimes.
    - ``binary``: the rsynced dist file plus the fused permission pass (1).

    ``clone_from`` is the live release directory; the caller decides
    eligibility (:func:`fujin.commands.deploy.venv_clone_eligible`). If the
    clone itself fails (e.g. the live venv vanished between the check and
    now) the full install runs instead, so a stale eligibility decision
    degrades to the previous behaviour instead of failing the deploy.

    The permission pass is one ``conn.run`` that sets the ``.env`` mode, the
    venv modes and the deploy-wide group/setgid model. ``venv/`` is only
    pruned from the *file* walk: its files already carry ``go+rX`` from the
    venv-specific ``chmod -R`` above, while its directories must still go
    through the privileged directory walk so they end up setgid (an
    unprivileged ``chmod`` strips ``S_ISGID`` on entries the deploy user does
    not own the group of).

    ``requirements`` is the local requirements.txt path; the build committed
    the same file into the staging tree that rsync uploaded, so it is never
    transferred here - the pip command only asserts it is present.
    """
    staging_q = shlex.quote(staging_dir)
    dist_in_tree = f"{staging_q}/{shlex.quote(dist_tree_path)}"
    clone_marker: str | None = None

    if installation_mode == "python-package":
        if not python_version:
            raise ServerOpsError("python mode requires a python_version")
        venv_q = f"{staging_q}/{_PYTHON_ROOT}"
        cloned = False
        if clone_from:
            live_venv = f"{shlex.quote(clone_from)}/{_PYTHON_ROOT}"
            _cp_out, cp_ok = conn.run(
                f"cp -al {live_venv} {venv_q}", warn=True, hide=True
            )
            cloned = cp_ok
            if not cloned:
                # A partially written clone must not be reused as an install
                # target; start over with the full install.
                conn.run(f"rm -rf {venv_q}", warn=True, hide=True)
        if cloned:
            # App package only: dependencies and every other file are the
            # live release's inodes. Compiling bytecode is safe for the wheel
            # just installed (new files); the marker created in the same
            # command bounds the permission pass to exactly those files.
            clone_marker = f"{staging_dir}/{CLONE_MARKER_PATH}"
            pip_cmd = (
                # The marker is backdated a second: filesystems with coarse
                # timestamps (tmpfs) can give the marker and the install's
                # first writes the *same* mtime, which ``-newer`` would miss.
                # Live-release files are always older than this deploy.
                f"touch {shlex.quote(clone_marker)}"
                f" && touch -d '1 second ago' {shlex.quote(clone_marker)} && "
                f"UV_PYTHON_INSTALL_DIR={UV_PYTHON_INSTALL_DIR} "
                f"uv pip install --python {venv_q}/bin/python "
                f"{dist_in_tree} --no-deps --compile-bytecode"
            )
            _pip_out, ok = conn.run(pip_cmd, warn=True)
            if not ok:
                raise ServerOpsError(
                    f"uv pip install into the cloned venv failed:\n{_pip_out.strip()}"
                )
        else:
            _, ok = conn.run(
                f"UV_PYTHON_INSTALL_DIR={UV_PYTHON_INSTALL_DIR} "
                f"uv venv --relocatable -p {shlex.quote(python_version)}"
                f" --managed-python {venv_q}",
                warn=True,
            )
            if not ok:
                raise ServerOpsError(
                    f"uv venv creation failed in {staging_dir}.\n"
                    "  - `uv venv --relocatable` is required (venv clone path);"
                    " an older uv may not support it\n"
                    "  - Upgrade uv on the server with: fujin server upgrade\n"
                    "  - Check the remote uv version with: fujin doctor"
                )
            pip_cmd = (
                f"UV_PYTHON_INSTALL_DIR={UV_PYTHON_INSTALL_DIR} UV_COMPILE_BYTECODE=1 "
                f"uv pip install --python {venv_q}/bin/python "
                f"{dist_in_tree} --no-deps"
            )
            if requirements:
                req_in_tree = f"{staging_dir}/requirements.txt"
                req_q = shlex.quote(req_in_tree)
                # The file rides in with the rsynced tree (an asset entry);
                # fail loudly here rather than letting uv report a missing
                # -r file.
                pip_cmd = (
                    f"test -f {req_q}"
                    f" || {{ echo 'fujin: {req_in_tree} missing from the staging"
                    f" tree'; exit 1; }}; "
                    f"{pip_cmd} -r {req_q}"
                )
            # A silent dependency install is not wedged just because it emits
            # nothing for minutes: give it a much later idle deadline than the
            # 300 s default (non-PTY, so no interactive prompts are expected).
            _pip_out, ok = conn.run(pip_cmd, warn=True, idle_timeout=900)
            if not ok:
                if "missing from the staging tree" in _pip_out:
                    raise ServerOpsError(
                        f"{staging_dir}/requirements.txt is missing from the"
                        " staging tree; the upload did not carry the build's"
                        " requirements entry"
                    )
                raise ServerOpsError(
                    f"uv pip install into staging venv failed:\n{_pip_out.strip()}"
                )
    elif installation_mode != "binary":
        raise ServerOpsError(f"Unknown installation mode: {installation_mode}")

    script = _staging_permissions_script(
        staging_dir,
        app_user,
        installation_mode,
        dist_in_tree,
        clone_marker=clone_marker,
    )
    out, ok = conn.run(script, warn=True)
    if ok:
        return
    raise _staging_permission_error(_failed_step(out), staging_dir)


#: Steps in the fused staging permission pass -> the error they report.
_STAGING_STEP_ERRORS: dict[str, str] = {
    "dist-mode": "Failed to chmod dist file in {staging}",
    "env-mode": "Failed to chmod .env in {staging}",
    "venv-mode": "Failed to set venv permissions in {staging}",
    "venv-exec": "Failed to set venv permissions in {staging}",
    "chgrp": "Failed to normalize ownership of {staging}",
    "dirs": "Failed to normalize ownership of {staging}",
    "files": "Failed to normalize ownership of {staging}",
}


def _staging_permission_error(step: str | None, staging_dir: str) -> ServerOpsError:
    template_ = _STAGING_STEP_ERRORS.get(step or "")
    if template_ is None:
        return ServerOpsError(f"Failed to prepare {staging_dir}")
    return ServerOpsError(template_.format(staging=staging_dir))


def _staging_permissions_script(
    staging_dir: str,
    app_user: str,
    installation_mode: str,
    dist_in_tree: str,
    *,
    clone_marker: str | None = None,
) -> str:
    """One shell script applying every staging mode/ownership rule.

    Modes are unchanged from the sequential implementation; only the
    round-trips are fused.

    ``venv/`` is pruned from the tree-wide *file* walk only: the
    venv-specific ``chmod -R`` above already applies ``u+rwX,go+rX`` there.
    Its directories must go through the privileged directory walk below,
    because they need the setgid bit and an unprivileged ``chmod`` on an
    entry the deploy user does not own the group of (the staging tree is
    ``deploy:app`` under a setgid parent) silently drops ``S_ISGID`` or
    fails outright.

    Directories are handled separately from files: a bit-adding ``chmod -R``
    over the whole tree would mutate the inodes hardlinked from the live
    release (``link_unchanged``), so only directories and *private* files
    (``-links 1``) are touched by the tree-wide passes.

    ``clone_marker`` is set on the venv-clone path: the venv was
    ``cp -al``-cloned from the live release, so every pre-existing venv
    *file* is a live inode. Recursive chmod/chgrp over it would rewrite the
    live release's modes/ctimes; the venv passes therefore only select
    entries newer than the marker (created immediately before the wheel
    install). Venv *directories* are private clone inodes and keep the
    regular walk. The marker itself is removed here.
    """
    staging_q = shlex.quote(staging_dir)
    venv_q = shlex.quote(f"{staging_dir}/{_PYTHON_ROOT}")
    lines = []
    if installation_mode == "python-package" and clone_marker is None:
        # Group-readable/executable venv, owner-writable (installer PHASE 2
        # permission model), applied BEFORE hashing so recorded modes match.
        lines += [
            f"chmod -R u+rwX,go+rX {staging_q}/{_PYTHON_ROOT} {_fail_marker('venv-mode')}",
            f"chmod -R u+x {staging_q}/{_PYTHON_ROOT}/bin {_fail_marker('venv-exec')}",
        ]
    elif installation_mode != "python-package":
        # The dist file ships via the rsynced tree; only its directory and
        # mode need enforcing. A failed mkdir surfaces through the chmod.
        lines.append(f"mkdir -p {staging_q}/{_BINARY_ROOT} || true")
        lines.append(f"chmod 755 {dist_in_tree} {_fail_marker('dist-mode')}")

    lines.append(f"chmod 0640 {staging_q}/{ENV_PATH} {_fail_marker('env-mode')}")
    if clone_marker is not None:
        marker_q = shlex.quote(clone_marker)
        lines += [
            # Venv directories are clone-private inodes (cp -al hardlinks
            # files only), so they keep the full venv mode normalization.
            f"sudo find {venv_q} -type d -exec chmod u+rwX,go+rX {{}} +"
            f" {_fail_marker('venv-mode')}",
            # Fresh tree entries (private inodes) plus the install's own venv
            # outputs; hardlinked live entries are never selected.
            f"sudo find {staging_q} -path {venv_q} -prune -o"
            f" \\( -type d -o -links 1 \\) -exec chgrp {shlex.quote(app_user)}"
            f" {{}} + {_fail_marker('chgrp')}",
            f"sudo find {venv_q} -newer {marker_q} -exec chgrp"
            f" {shlex.quote(app_user)} {{}} + {_fail_marker('chgrp')}",
            f"sudo find {venv_q} -newer {marker_q} -type f -exec chmod"
            f" u+rwX,go+rX {{}} + {_fail_marker('venv-mode')}",
            f"sudo find {venv_q}/bin -newer {marker_q} -type f -exec chmod u+x"
            f" {{}} + {_fail_marker('venv-exec')}",
            # Directories (venv included) are clone-private inodes.
            f"sudo find {staging_q} -type d"
            f" -exec chmod g+rxs {{}} + {_fail_marker('dirs')}",
            f"sudo find {staging_q} -path {venv_q} -prune -o -links 1 -type f"
            f" -exec chmod g+rX {{}} + {_fail_marker('files')}",
            f"rm -f {marker_q}",
        ]
        return "\n".join(lines)

    lines += [
        f"sudo chgrp -R {shlex.quote(app_user)} {staging_q} {_fail_marker('chgrp')}",
        # Directories (venv included): group-traversable, and setgid so late
        # writes (hooks, runtime) keep inheriting the group.
        f"sudo find {staging_q} -type d"
        f" -exec chmod g+rxs {{}} + {_fail_marker('dirs')}",
        # Non-hardlinked files only; venv files already got go+rX above.
        f"sudo find {staging_q} -path {venv_q} -prune -o -links 1 -type f"
        f" -exec chmod g+rX {{}} + {_fail_marker('files')}",
    ]
    return "\n".join(lines)


def hash_server_built(
    conn: Any,
    staging_dir: str,
    installation_mode: str,
    *,
    live_release_dir: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Hash server-built entries (``venv/**`` or ``bin/**``) on the remote host.

    Returns ``{path: {"sha256": hex, "mode": int}}`` for regular files and
    ``{path: {"link": target, "mode": int}}`` for symlinks, keyed by
    staging-relative paths. Raises on a non-zero exit or undecodable output.

    ``live_release_dir`` is the live release the staging tree was cloned from
    (see :func:`prepare_staging`). The server script reads that release's
    ``manifest.json`` and, for every staged file whose ``(st_dev, st_ino)``
    equals the live file's, reuses the recorded sha256 instead of re-reading
    the bytes: a hardlinked file is the same inode, so it cannot differ. Only
    new/changed files (the installed app package, new metadata) are read.
    """
    root = {
        "python-package": _PYTHON_ROOT,
        "binary": _BINARY_ROOT,
    }.get(installation_mode)
    if root is None:
        raise ServerOpsError(f"Unknown installation mode: {installation_mode}")

    root_path = f"{staging_dir}/{root}"
    body = "\n".join(
        [
            "import hashlib, json, os, stat",
            "root = " + repr(root_path),
            "live = " + repr(live_release_dir),
            "reusable = {}",
            "if live:",
            "    # Only entries under this tree can be reused; a stale or",
            "    # unreadable live manifest just means hashing everything.",
            "    try:",
            "        with open(os.path.join(live, 'manifest.json'),"
            " encoding='utf-8') as fh:",
            "            data = json.load(fh)",
            "        for rel, entry in (data.get('entries') or {}).items():",
            "            sha = entry.get('sha256')",
            "            if sha and rel.startswith(" + repr(root + "/") + "):",
            "                reusable[rel] = sha",
            "    except (OSError, ValueError, AttributeError):",
            "        reusable = {}",
            "result = {}",
            "base = os.path.dirname(root)",
            "for dirpath, dirnames, filenames in os.walk(root):",
            "    for name in dirnames + filenames:",
            "        path = os.path.join(dirpath, name)",
            "        rel = os.path.relpath(path, base)",
            "        if os.path.islink(path):",
            "            result[rel] = {'link': os.readlink(path), 'mode': 0o777}",
            "        elif os.path.isfile(path):",
            "            st = os.lstat(path)",
            "            sha = reusable.get(rel) if live else None",
            "            if sha:",
            "                try:",
            "                    lst = os.lstat(os.path.join(live, rel))",
            "                except OSError:",
            "                    lst = None",
            "                # Same inode => same bytes: reuse the live entry's",
            "                # digest instead of reading the file again.",
            "                if lst is None or (lst.st_dev, lst.st_ino) != (",
            "                    st.st_dev, st.st_ino",
            "                ):",
            "                    sha = None",
            "            if not sha:",
            "                digest = hashlib.sha256()",
            "                with open(path, 'rb') as fh:",
            "                    for chunk in iter(lambda: fh.read(131072), b''):",
            "                        digest.update(chunk)",
            "                sha = digest.hexdigest()",
            "            result[rel] = {'sha256': sha,"
            " 'mode': stat.S_IMODE(st.st_mode)}",
            "print(json.dumps(result))",
        ]
    )
    out, ok = conn.run(_python_script(body), warn=True, hide=True)
    if not ok:
        raise ServerOpsError("Server-side hashing of venv/bin tree failed")
    try:
        return json.loads(out.strip())
    except (json.JSONDecodeError, ValueError) as exc:
        raise ServerOpsError(f"Undecodable hash output: {exc}") from exc


def link_unchanged(
    conn: Any, staging_dir: str, prev_release_dir: str, paths: dict[str, int]
) -> tuple[int, list[str]]:
    """Pre-hardlink unchanged files into *staging* from the previous release.

    ``paths`` maps staging-relative paths to the manifest mode to apply.
    Best effort: failures are returned and the rsync that follows transfers
    whatever is missing. Returns ``(linked_count, failed_paths)``.

    This replaces rsync's ``--link-dest``: the 2024 CVE fixes break both
    symlinked basis paths and whole-file verification of updated files
    (RsyncProject/rsync#715, exit 23 "update discarded"), so dedup is done
    explicitly here instead.
    """
    if not paths:
        return 0, []

    body = "\n".join(
        [
            "import json, os",
            "staging = " + repr(staging_dir),
            "prev = " + repr(prev_release_dir),
            "entries = json.loads(" + repr(json.dumps(paths)) + ")",
            "linked, failed = 0, []",
            "for rel, mode in entries.items():",
            "    src = os.path.join(prev, rel)",
            "    dst = os.path.join(staging, rel)",
            "    try:",
            "        # A chmod on a hardlink mutates the shared inode, i.e. the"
            " live release. Only link files whose on-disk mode already"
            " matches; anything else is left for rsync to copy privately.",
            "        if (os.stat(src).st_mode & 0o777) != mode:",
            "            failed.append(rel)",
            "            continue",
            "        os.makedirs(os.path.dirname(dst), exist_ok=True)",
            "        os.link(src, dst)",
            "        linked += 1",
            "    except OSError:",
            "        failed.append(rel)",
            "print(json.dumps({'linked': linked, 'failed': failed}))",
        ]
    )
    out, ok = conn.run(_python_script(body), warn=True, hide=True)
    if not ok:
        return 0, sorted(paths)
    try:
        parsed = json.loads(out.strip())
        return int(parsed["linked"]), sorted(parsed["failed"])
    except (json.JSONDecodeError, ValueError, KeyError, TypeError) as exc:
        raise ServerOpsError(f"Undecodable link output: {exc}") from exc


# =============================================================================
# Systemd units
# =============================================================================


def install_units(
    conn: Any,
    app_name: str,
    unit_files: Mapping[str, str],
    dropins: Mapping[str, Mapping[str, str]] | None = None,
    *,
    changed: set[str] | None = None,
) -> None:
    """Install unit files (service/socket/timer) + dropins into
    ``/etc/systemd/system``, remove stale units of this app, and
    ``daemon-reload`` - all in ONE remote script.

    ``unit_files`` maps the unit file name (e.g. ``myapp-web@.service``) to
    its rendered content. ``dropins`` maps a unit name to
    ``{dropin-filename: content}``; the caller expands ``common.d`` per unit.

    ``changed`` (optional) is the set of unit/dropin tree names whose content
    changed in this release; only those are (re)written. ``valid``/stale
    scanning and the single ``daemon-reload`` always cover the full declared
    set, so unchanged units keep their installed files untouched. ``None``
    means "write everything" (first deploy, ``--force``, repair paths).

    The script starts with the two read-only scans (installed units at depth 1
    plus dropin *files* at depth 2, and empty declared ``*.d`` dirs) so
    stale/prune decisions see the pre-write state; both are printed between
    markers for diagnostics. Every mutating step keeps the exact per-file
    ``sudo`` shapes the scoped sudoers rule matches and appends a
    ``FUJIN-FAIL`` marker, so a failure names the
    file/step it aborted on.
    """
    dropins = dropins or {}
    units = set(unit_files) | set(dropins)
    valid = units | {f"{unit}.d" for unit in units}
    # Dropin *files* are valid too: kept ``*.d/`` dirs must not accumulate
    # conf files from an older release (pruned at depth 2 below).
    valid |= {f"{unit}.d/{name}" for unit, files in dropins.items() for name in files}
    # A unit that no longer declares dropins must not keep an empty
    # ``<unit>.d`` directory behind (systemd treats it as present, and a
    # stale empty dir confuses ``installed_unit_tree`` consumers).
    prune = {f"{unit}.d" for unit in units} - {
        f"{unit}.d" for unit, files in dropins.items() if files
    }

    write_units = (
        unit_files
        if changed is None
        else {name: content for name, content in unit_files.items() if name in changed}
    )
    write_dropins: dict[str, dict[str, str]] = {}
    for unit, files in dropins.items():
        selected = {
            name: content
            for name, content in files.items()
            if changed is None or f"{unit}.d/{name}" in changed
        }
        if selected:
            write_dropins[unit] = selected

    out, ok = conn.run(
        _install_units_script(app_name, write_units, write_dropins, valid, prune),
        warn=True,
        hide=True,
    )
    if ok:
        return
    step, _, target = (_failed_step(out) or "").partition(":")
    if step == "daemon-reload":
        raise ServerOpsError("systemctl daemon-reload failed")
    if step == "mkdir":
        raise ServerOpsError(f"Failed to create {target} on server")
    if step == "write":
        raise ServerOpsError(f"Failed to write {target} on server")
    if step == "cd":
        raise ServerOpsError(f"Failed to inspect {SYSTEMD_DIR} on server")
    raise ServerOpsError(f"Failed to install systemd units for {app_name}")


def _case_alternatives(names: Iterable[str]) -> str:
    """``case`` alternatives matching *names* literally (quoted, no globs)."""
    patterns = [shlex.quote(name) for name in sorted(names)]
    # An empty alternative matches the empty string only, i.e. never.
    return "|".join(patterns) if patterns else "''"


def _stale_unit_lines(valid: set[str]) -> list[str]:
    """Shell loop disabling/removing installed entries not in *valid*.

    Covers both depth 1 (``app-*.service``, ``app-*.socket``, ``app-*.timer``,
    ``app-*.d`` dirs) and depth 2 (files inside a kept ``app-*.d`` dir). A
    stale dropin file must go even when its directory remains in use.
    ``find``'s output was captured *before* the writes, so this loop only
    sees entries that were installed when the script started.
    """
    return [
        "printf '%s\\n' \"$installed\" | while IFS= read -r name; do",
        '  [ -n "$name" ] || continue',
        "  rel=${name#./}",
        '  case "$rel" in',
        f"    {_case_alternatives(valid)}) ;;",
        f'    */*) sudo rm -rf "{SYSTEMD_DIR}/$rel" || true ;;',
        "    *@.service)",
        '      sudo systemctl disable --quiet "$rel" || true',
        '      sudo systemctl reset-failed "$rel" || true',
        f'      sudo rm -rf "{SYSTEMD_DIR}/$rel" || true ;;',
        "    *)",
        '      sudo systemctl disable --now --quiet "$rel" || true',
        '      sudo systemctl reset-failed "$rel" || true',
        f'      sudo rm -rf "{SYSTEMD_DIR}/$rel" || true ;;',
        "  esac",
        "done",
    ]


def _install_units_script(
    app_name: str,
    unit_files: Mapping[str, str],
    dropins: Mapping[str, Mapping[str, str]],
    valid: set[str],
    prune: set[str],
) -> str:
    """The fused install script executed by :func:`install_units`."""
    # Two scans, because a single depth-2 ``-name '<app>*'`` also matched the
    # enablement symlinks systemd owns (``./multi-user.target.wants/<app>*.service``)
    # and the stale-removal loop then deleted them as "stale" (breaking boot
    # enablement). Depth 1 covers units and dropin dirs; depth 2 is limited to
    # files inside an ``<app>*.d`` dropin dir.
    installed_scan = (
        f"{{ find . -maxdepth 1 -name {shlex.quote(app_name + '*')} -print;"
        f" find . -maxdepth 2 -path {shlex.quote(f'./{app_name}*.d/*')} -print; }}"
        " 2>/dev/null | sort"
    )
    lines = [
        f"cd {shlex.quote(SYSTEMD_DIR)} {_fail_marker('cd')}",
        f"installed=$({installed_scan})",
        "printf '%s\\n' \"$installed\"",
        f"printf '%s\\n' {shlex.quote(INSTALL_INSTALLED_END)}",
    ]
    # The dropin directory is not shipped as a unit file, so create it before
    # writing into it (``mkdir -p`` is idempotent).
    for unit, files in dropins.items():
        if not files:
            continue
        dir_path = f"{SYSTEMD_DIR}/{unit}.d"
        lines.append(
            f"sudo mkdir -p {shlex.quote(dir_path)} {_fail_marker('mkdir', dir_path)}"
        )
    for name, content in unit_files.items():
        path = f"{SYSTEMD_DIR}/{name}"
        lines.append(
            f"{_sudo_write_command(path, content)} {_fail_marker('write', path)}"
        )
    for unit, files in dropins.items():
        for name, content in files.items():
            path = f"{SYSTEMD_DIR}/{unit}.d/{name}"
            lines.append(
                f"{_sudo_write_command(path, content)} {_fail_marker('write', path)}"
            )
    lines += _stale_unit_lines(valid)
    if prune:
        # Empty dirs are scanned *after* the stale removal above: a dir whose
        # last file was just pruned must be seen as empty.
        lines += [
            "empty=$(find . -maxdepth 1 -type d"
            f" -name {shlex.quote(app_name + '*.d')} -empty -print 2>/dev/null"
            " | sort)",
            "printf '%s\\n' \"$empty\"",
            f"printf '%s\\n' {shlex.quote(INSTALL_EMPTY_END)}",
            "printf '%s\\n' \"$empty\" | while IFS= read -r name; do",
            '  [ -n "$name" ] || continue',
            "  rel=${name#./}",
            '  case "$rel" in',
            f"    {_case_alternatives(prune)})"
            f' sudo rm -rf "{SYSTEMD_DIR}/$rel" || true ;;',
            "  esac",
            "done",
        ]
    lines.append(f"sudo systemctl daemon-reload {_fail_marker('daemon-reload')}")
    return "\n".join(lines)


def installed_unit_tree(conn: Any, app_name: str) -> dict[str, str | None]:
    """Read the units + dropins installed for *app_name* (read-only).

    Returns ``{relative path: content}``; directory entries (dropin dirs)
    map to ``None``. Paths are relative to ``/etc/systemd/system``. An
    unreadable file also maps to ``None`` (it is then reported as drifted by
    the caller, never silently ignored).
    """
    parent = SYSTEMD_DIR
    pattern = f"{app_name}*"
    dropin_glob = f"./{app_name}*.d/*"
    # Two scans on purpose: a depth-2 `-name '<app>*'` would also match
    # `multi-user.target.wants/<app>-web.service` (enablement symlinks are not
    # unit files and must never be reported as stray or removed).
    cmd = (
        f"cd {shlex.quote(parent)} 2>/dev/null && "
        f"{{ find . -maxdepth 1 -name {shlex.quote(pattern)} -print; "
        f"find . -maxdepth 2 -path {shlex.quote(dropin_glob)} -print; }}"
        f" 2>/dev/null | sort"
    )
    out, _ok = conn.run(cmd, warn=True, hide=True)
    entries = [
        line.strip().removeprefix("./")
        for line in out.splitlines()
        if line.strip() and line.strip() != "."
    ]
    tree: dict[str, str | None] = {}
    for rel in entries:
        if rel.endswith(".d"):
            tree[rel] = None
            continue
        content, ok = conn.run(
            f"cat {shlex.quote(f'{parent}/{rel}')}", warn=True, hide=True
        )
        tree[rel] = content if ok else None
    return tree


# =============================================================================
# Caddyfile (caddy.py reload-with-restore contract, client-driven)
# =============================================================================


def install_caddyfile(conn: Any, content: str, caddy_config_path: str) -> bool:
    """Install a Caddy config, reload Caddy, restore the previous config on
    failure.

    The previous config is also kept in a sibling ``<path>.fujin-bak`` file
    before the new one is written, so a crash mid-write never leaves a broken
    main config without a recovery path: a later call restores it.

    When the installed config is already byte-identical to *content* and no
    backup is left behind, the function is a no-op (no backup write, no
    rewrite, no reload) and True is returned immediately.

    A leftover ``.fujin-bak`` means a previous run died mid-install. If the
    main file already matches *content* the install itself is complete, but
    Caddy's loaded state is unknown: Caddy is reloaded and, on success, only
    the stale backup is removed (the main file is not rewritten). A reload
    failure keeps both files and returns False.

    A config restored from a leftover ``.fujin-bak`` is *not* a no-op even
    when it matches *content*: the restore then takes the normal success
    path (write, reload) and clears the stale backup.

    Returns True on successful reload. On failure the previous config (if
    any) is restored to disk without reloading (Caddy never loaded the new
    config) and False is returned.
    """
    path_q = shlex.quote(caddy_config_path)
    bak_path = f"{caddy_config_path}.fujin-bak"
    bak_q = shlex.quote(bak_path)

    _, main_exists = conn.run(f"test -f {path_q}", warn=True, hide=True)
    _, bak_exists = conn.run(f"test -f {bak_q}", warn=True, hide=True)

    # Crash recovery sweep: a previous run died between the backup and the
    # main write. Restore the backup before touching anything else; the
    # restore forces the reload path below.
    restored = False
    if bak_exists and not main_exists:
        conn.run(
            f"sudo cp {bak_q} {path_q} && sudo chown caddy:caddy {path_q}",
            warn=True,
            hide=True,
        )
        main_exists = True
        restored = True

    old_content: str | None = None
    if main_exists:
        out, ok = conn.run(f"cat {path_q}", warn=True, hide=True)
        old_content = out if ok else None

    if old_content == content and not restored:
        if not bak_exists:
            # The server already serves byte-identical content: no backup,
            # no rewrite, no reload (a reload would restart Caddy for nothing).
            logger.info("Caddy config at %s is already current; skipping", path_q)
            return True
        # Crash recovery: the main file was fully written but the run died
        # before the reload/cleanup. Do not rewrite the main file; reload so
        # Caddy's loaded state matches the disk, then drop the stale backup.
        logger.info(
            "Caddy config at %s is current but a stale backup exists; reloading",
            path_q,
        )
        _, ok = conn.run("sudo systemctl reload caddy", warn=True)
        if ok:
            conn.run(f"sudo rm -f {bak_q}", warn=True, hide=True)
            return True
        logger.warning(
            "Caddy reload failed; keeping %s and its recovery backup", path_q
        )
        return False

    if old_content is not None:
        _sudo_write(conn, bak_path, old_content, chown="caddy:caddy")

    _sudo_write(conn, caddy_config_path, content, chown="caddy:caddy")

    _, ok = conn.run("sudo systemctl reload caddy", warn=True)
    if ok:
        if restored:
            # The restored config is now written and loaded; the leftover
            # backup from the crashed run is no longer a recovery candidate.
            conn.run(f"sudo rm -f {bak_q}", warn=True, hide=True)
        return True

    logger.warning("Caddy reload failed; restoring previous configuration")
    if old_content is not None:
        _sudo_write(conn, caddy_config_path, old_content, chown="caddy:caddy")
    elif bak_exists:
        conn.run(f"sudo cp {bak_q} {path_q}", warn=True, hide=True)
    else:
        conn.run(f"sudo rm -f {path_q}", warn=True, hide=True)
    return False


def caddyfile_fingerprint(conn: Any, caddy_config_path: str) -> tuple[str | None, bool]:
    """Probe the installed Caddy config in one round trip.

    Returns ``(sha256_hex_or_None, backup_exists)``: the digest of the
    installed file (None when it is absent or the probe failed) and whether
    the crash-recovery sibling ``<path>.fujin-bak`` is present.
    """
    path_q = shlex.quote(caddy_config_path)
    bak_q = shlex.quote(f"{caddy_config_path}.fujin-bak")
    cmd = (
        f"if [ -f {path_q} ]; then sha256sum {path_q} | cut -d' ' -f1;"
        " else echo MISSING; fi;"
        f" if [ -f {bak_q} ]; then echo BAK; else echo NOBAK; fi"
    )
    out, ok = conn.run(cmd, warn=True, hide=True)
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    if not ok or len(lines) != 2:
        return None, False
    sha = None if lines[0] == "MISSING" else lines[0]
    return sha, lines[1] == "BAK"


# =============================================================================
# Store state / manifest reads + server-side atomic writes
# =============================================================================


def read_state(conn: Any, app_dir: str) -> State | None:
    """Read ``store/state.json``; a missing, unreadable, or corrupt file
    yields None."""
    out, ok = conn.run(
        f"cat {shlex.quote(f'{app_dir}/{STATE_PATH}')}", warn=True, hide=True
    )
    if not ok:
        return None
    try:
        return parse_state(out)
    except (msgspec.DecodeError, ValueError):
        logger.warning("Corrupt state.json in %s; treating as absent", app_dir)
        return None


def read_symlink(conn: Any, path: str) -> str | None:
    """Resolve *path* once, read-only; None when absent or not a symlink."""
    out, ok = conn.run(f"readlink {shlex.quote(path)}", warn=True, hide=True)
    target = out.strip() if ok and out else ""
    return target or None


def path_stat(conn: Any, path: str) -> tuple[int, str, str] | None:
    """``(mode, user, group)`` of *path*, or None when it cannot be stat'ed."""
    out, ok = conn.run(f"stat -c '%a %U %G' {shlex.quote(path)}", warn=True, hide=True)
    fields = out.split() if ok and out else []
    if len(fields) != 3:
        return None
    mode, owner, group = fields
    try:
        return int(mode, 8), owner, group
    except ValueError:
        return None


def write_state(conn: Any, app_dir: str, state: State) -> None:
    """Persist state server-side: write ``state.json.tmp`` then rename."""
    path = f"{app_dir}/{STATE_PATH}"
    tmp = f"{app_dir}/{STATE_PATH}.tmp"
    cmd = (
        f"printf '%s' {_b64(serialize_state(state))}"
        f" | base64 -d > {shlex.quote(tmp)} && mv -f {shlex.quote(tmp)} {shlex.quote(path)}"
    )
    _, ok = conn.run(cmd, warn=True, hide=True)
    if not ok:
        raise ServerOpsError(f"Failed to write {path} on server")


def read_manifest_at(conn: Any, release_dir: str) -> m.Manifest | None:
    """Decode the manifest of *release_dir*; None when absent or corrupt."""
    raw = read_remote_file(conn, f"{release_dir}/{MANIFEST_PATH}")
    if raw is None:
        return None
    try:
        return m.decode_manifest(raw)
    except Exception:
        logger.warning("Undecodable manifest in %s", release_dir)
        return None


def read_current_manifest(conn: Any, app_dir: str) -> m.Manifest | None:
    """Decode the manifest of the live release; None when absent.

    Uses SCP (not ``cat``): the merged manifest can be hundreds of KB and
    big exec-channel reads can trickle forever (see SSH2Connection.get).
    """
    return read_manifest_at(conn, f"{app_dir}/current")


# =============================================================================
# Template-instance convergence plan
# =============================================================================


def _template_instances(lines: Iterable[str]) -> set[str]:
    """Instance unit names reported by a ``systemctl`` listing (non-fatal).

    Both the enabled and the active view are queried: enablement alone misses
    an instance somebody stopped by hand, runtime state alone misses an
    enabled instance that is not running. Bare templates are dropped.
    """
    instances: set[str] = set()
    for line in lines:
        parts = line.split()
        if not parts:
            continue
        name = parts[0]
        if not name.endswith(".service") or name.endswith("@.service"):
            continue
        instances.add(name)
    return instances


def _linked_template_instances(lines: Iterable[str]) -> set[str]:
    """Instances enabled on disk, derived from ``*.wants``/``*.requires`` links.

    ``systemctl list-unit-files`` lists the template unit file (``svc@.service``)
    but never its instances, so it cannot tell whether ``svc@2`` is enabled.
    ``systemctl enable`` records enablement as a symlink inside a target's
    ``.wants``/``.requires`` directory, which this read-only scan finds - that
    is what makes a manually stopped (but still enabled) instance visible.
    """
    return {
        name
        for name in (line.strip() for line in lines)
        if name.endswith(".service") and not name.endswith("@.service")
    }


def _split_plan_sections(output: str) -> tuple[list[str], list[str], list[str]]:
    """Split the fused plan output at its end-of-section markers.

    Yields ``(enabled listing, ``.wants`` links, active listing)``. A missing
    marker leaves the remaining lines in the section they started in, so a
    truncated probe degrades instead of misattributing lines.
    """
    sections: tuple[list[str], list[str], list[str]] = ([], [], [])
    index = 0
    for line in output.splitlines():
        text = line.strip()
        if text == PLAN_ENABLED_END:
            index = 1
        elif text == PLAN_LINKS_END:
            index = 2
        elif text:
            sections[index].append(text)
    return sections


def template_instance_plan(
    conn: Any, app_name: str, desired: Iterable[str]
) -> tuple[list[str], list[str]]:
    """Converge plan for template instances of *app_name*.

    *desired* holds instance ids (``svc@1``) or unit names
    (``svc@1.service``). Returns ``(start_specs, stop_specs)`` as full unit
    names: ``start_specs`` sorted ascending covers instances that are missing,
    stopped-but-enabled or running-but-disabled; ``stop_specs`` are the
    present instances not desired, highest instance number first.

    The three read-only queries (enabled listing, ``.wants``/``.requires``
    symlink scan, active listing) run as one ``conn.run`` separated by
    markers; the decision itself stays in :func:`store.convergence`.
    """
    pattern = f"{app_name}-*@*.service"
    pattern_q = shlex.quote(pattern)
    out, _ok = conn.run(
        f"systemctl list-unit-files --no-legend {pattern_q} 2>/dev/null || true\n"
        f"printf '%s\\n' {shlex.quote(PLAN_ENABLED_END)}\n"
        f"find {shlex.quote(SYSTEMD_DIR)} -maxdepth 2 -type l -name {pattern_q}"
        f" -printf '%f\\n' 2>/dev/null || true\n"
        f"printf '%s\\n' {shlex.quote(PLAN_LINKS_END)}\n"
        f"systemctl list-units --type=service --state=active --no-legend"
        f" {pattern_q} 2>/dev/null || true",
        warn=True,
        hide=True,
    )
    enabled_lines, link_lines, active_lines = _split_plan_sections(out)
    enabled = _template_instances(enabled_lines) | _linked_template_instances(
        link_lines
    )
    active = _template_instances(active_lines)

    desired_units: set[str] = set()
    for unit in desired:
        name = unit.removesuffix(".service")
        if "@" not in name or name.endswith("@"):
            continue
        desired_units.add(f"{name}.service")

    start_ids, stop_ids = convergence(desired_units, enabled, active)
    start = sorted(f"{name}.service" for name in start_ids)
    stop = sorted(
        (f"{name}.service" for name in stop_ids),
        key=lambda unit: (-instance_sort_key(unit), unit),
    )
    return start, stop


# =============================================================================
# Thin enable/start & stop/disable wrappers (convergence callsites)
# =============================================================================


def enable_and_start(conn: Any, units: Sequence[str]) -> bool:
    """Enable and start units (``sudo systemctl enable --now``). No-op on
    empty input."""
    if not units:
        return True
    _, ok = conn.run(f"sudo systemctl enable --now {q(*units)}", warn=True)
    return ok


def stop_and_disable(conn: Any, units: Sequence[str]) -> bool:
    """Stop and disable units (``systemctl disable --now``). No-op on empty
    input; template instances converge through the same command."""
    if not units:
        return True
    _, ok = conn.run(f"sudo systemctl disable --now {q(*units)}", warn=True)
    if ok:
        conn.run(f"sudo systemctl reset-failed {q(*units)}", warn=True, hide=True)
    return ok


# =============================================================================
# Post-restart readiness probe
# =============================================================================

_TYPE_NOTIFY_RE = re.compile(r"^\s*Type\s*=\s*notify\s*$", re.IGNORECASE | re.MULTILINE)
_LISTEN_STREAM_RE = re.compile(
    r"^\s*ListenStream\s*=\s*(\S+)\s*$", re.IGNORECASE | re.MULTILINE
)


def notify_units(unit_files: Mapping[str, str]) -> set[str]:
    """Service units in a release tree that declare ``Type=notify``.

    ``systemctl restart`` on a notify unit blocks until the unit sends
    ``READY=1``; the command fails when that does not happen within its
    timeout. A notify unit therefore never reaches the ``is-failed`` poll as
    a "maybe still starting" unit: a hung notify unit has already failed the
    restart, which routes through the same failure/rollback path. Polling it
    again would only duplicate that verdict.
    """
    return {
        name
        for name, content in unit_files.items()
        if name.endswith(".service") and _TYPE_NOTIFY_RE.search(content)
    }


def socket_paths(
    unit_files: Mapping[str, str],
    dropins: Mapping[str, Mapping[str, str]] | None = None,
) -> dict[str, str]:
    """Absolute ``ListenStream=`` paths declared by socket units.

    Scans the socket unit files *and* their ``*.socket.d/*.conf`` dropins
    (``dropins`` is keyed by unit name, the :func:`expand_dropins` contract).
    Returns ``{path: socket_unit_name}``. Abstract (``@...``) and relative
    addresses are ignored: only an absolute filesystem path can be probed
    with ``test -S``.
    """
    paths: dict[str, str] = {}

    def scan(name: str, contents: Iterable[str]) -> None:
        for content in contents:
            for match in _LISTEN_STREAM_RE.finditer(content):
                path = match.group(1)
                if path.startswith("/"):
                    paths[path] = name

    for name, content in unit_files.items():
        if name.endswith(".socket"):
            scan(name, [content])
    for unit, files in (dropins or {}).items():
        if unit.endswith(".socket"):
            scan(unit, files.values())
    return paths


def _is_notify(unit: str, notify: set[str]) -> bool:
    """True when *unit* (a concrete instance) is backed by a ``Type=notify``
    unit file, i.e. ``worker@1.service`` for ``worker@.service``."""
    if unit in notify:
        return True
    if "@" in unit:
        template = f"{unit.rsplit('@', 1)[0]}@.service"
        if template in notify:
            return True
    return False


def readiness_units(
    manifest: m.Manifest,
    *,
    app_name: str,
    replicas: Mapping[str, int],
    oneshots: Iterable[str],
    notify: Iterable[str] = (),
) -> list[str]:
    """Runtime service units to health-check, derived from a release manifest.

    Same semantics the old installer used: oneshots and services that have a
    socket or timer in the tree are exempt (the socket/timer governs their
    lifecycle); template services poll their ``svc@1..N`` instances.
    ``Type=notify`` units are excluded: ``systemctl restart`` already blocked
    on their ``READY=1``, so a failed/timed-out activation was caught there.
    """
    oneshots = set(oneshots)
    notify = set(notify)
    unit_names: set[str] = set()
    for path, entry in manifest.entries.items():
        if entry.kind is not m.EntryKind.UNIT:
            continue
        if name := unit_entry_name(path):
            unit_names.add(name)
    units_to_check: list[str] = []
    app_prefix = f"{app_name}-"
    for name in sorted(unit_names):
        if not name.endswith(".service"):
            continue
        if name in oneshots:
            continue
        if _is_notify(name, notify):
            continue
        base = name.removesuffix(".service").removesuffix("@")
        if f"{base}.socket" in unit_names or f"{base}.timer" in unit_names:
            continue
        if name.endswith("@.service"):
            service = name.removesuffix("@.service")
            if not service.startswith(app_prefix):
                continue
            count = replicas.get(service[len(app_prefix) :], 1)
            units_to_check.extend(
                sorted(f"{i}.service" for i in desired_instances(service, count))
            )
        else:
            units_to_check.append(name)
    return units_to_check


def wait_for_units(
    conn: Any,
    units: Sequence[str],
    sockets: Mapping[str, str],
    *,
    timeout: float | None = None,
    interval: float | None = None,
    clock: Any = None,
) -> list[str]:
    """Poll until every unit/socket is ready; return the offenders.

    A runtime service unit is ready only once ``systemctl is-failed`` reports
    ``active`` for it; ``activating``, ``inactive`` and empty output all mean
    "not ready yet" and are retried. A ``failed`` unit fails immediately.
    For a socket-activated service readiness requires BOTH the socket file to
    exist (``test -S``) and the owning socket unit to report ``active``.

    The window defaults to :data:`READINESS_TIMEOUT` seconds and the poll
    interval to :data:`READINESS_INTERVAL`; ``clock`` is a module-like object
    providing ``monotonic`` and ``sleep`` (tests inject a fake one). Returns
    an empty list when everything became ready, otherwise the units that
    failed / never reached ``active``. The caller owns the failure policy
    (deploy rolls back, rollback exits 6).
    """
    units = list(units)
    sockets = dict(sockets)
    if not units and not sockets:
        return []
    timeout = READINESS_TIMEOUT if timeout is None else timeout
    interval = READINESS_INTERVAL if interval is None else interval
    clock = time if clock is None else clock
    probe_command = readiness_probe_command(units, sockets)
    deadline = clock.monotonic() + timeout
    while True:
        # A status probe is cheap and idempotent: retry a stalled exec
        # channel once instead of stalling the whole activation.
        for attempt in range(2):
            try:
                out, _ok = conn.run(
                    probe_command, warn=True, hide=True, idle_timeout=15
                )
                break
            except ConnectionError:
                if attempt == 1:
                    raise
        statuses, socket_states = parse_readiness_probe(out)
        failed = [
            unit
            for unit, status in zip(units, statuses, strict=False)
            if status == "failed"
        ]
        if failed:
            return failed
        not_ready = [
            unit
            for unit, status in zip(units, statuses, strict=False)
            if status != "active"
        ]
        # A socket file legitimately appears only once the socket unit is
        # active; the probe also reports socket units that are activating
        # but not yet bound.
        unready_sockets: list[str] = []
        for unit in sockets.values():
            if not socket_states.get(unit, False) and unit not in unready_sockets:
                unready_sockets.append(unit)
        if not not_ready and not unready_sockets:
            return []
        if clock.monotonic() >= deadline:
            # Report every unit that never reached active, services and
            # socket units alike.
            return [*not_ready, *unready_sockets]
        clock.sleep(interval)


def readiness_probe_command(units: Sequence[str], sockets: Mapping[str, str]) -> str:
    """One read-only command answering every readiness question of a tick.

    ``systemctl is-failed`` statuses come first (one line per unit, in
    *units* order), then one ``FUJIN-SOCKET <unit>`` block per socket with
    its ``test -S`` verdict and - only when the socket file exists - its
    ``systemctl is-active`` state. The deploy command polls this and routes
    failures exactly as before; no sudo (the deploy user reads its own
    units).
    """
    lines = []
    if units:
        lines.append(f"systemctl is-failed {q(*units)} 2>/dev/null || true")
    lines.append(f"printf '%s\\n' {shlex.quote(READINESS_STATUS_END)}")
    for path, unit in sockets.items():
        lines += [
            f"printf '%s\\n' {shlex.quote(f'{READINESS_SOCKET_BEGIN} {unit}')}",
            f"if test -S {shlex.quote(path)}; then"
            f" printf '%s\\n' present;"
            f" systemctl is-active {shlex.quote(unit)} 2>/dev/null || true;"
            f" else printf '%s\\n' missing; fi",
            f"printf '%s\\n' {shlex.quote(READINESS_SOCKET_END)}",
        ]
    return "\n".join(lines)


def parse_readiness_probe(output: str) -> tuple[list[str], dict[str, bool]]:
    """Split fused probe *output* into statuses and socket verdicts.

    Returns ``(statuses, {socket unit: bound and active})``. Statuses are in
    the order the units were passed to :func:`readiness_probe_command`; a
    socket without a complete block is absent from the mapping (callers treat
    that as not ready, like ``inactive``).

    A socket unit may own several ``ListenStream=`` paths (one block each,
    same unit name), so verdicts are ANDed per unit: the unit is ready only
    when *every* one of its paths is bound and the unit is active.
    """
    lines = [line.strip() for line in output.splitlines()]
    statuses: list[str] = []
    index = 0
    while index < len(lines) and lines[index] != READINESS_STATUS_END:
        if lines[index]:
            statuses.append(lines[index])
        index += 1
    sockets: dict[str, bool] = {}
    unit: str | None = None
    present = active = False
    for line in lines[index + 1 :]:
        if line == READINESS_SOCKET_END:
            if unit is not None:
                verdict = present and active
                sockets[unit] = sockets.get(unit, True) and verdict
            unit = None
        elif line.startswith(f"{READINESS_SOCKET_BEGIN} "):
            unit = line[len(READINESS_SOCKET_BEGIN) :].strip()
            present = active = False
        elif unit is not None:
            if line == "present":
                present = True
            elif line == "active":
                active = True
    return statuses, sockets


# =============================================================================
# Post-start health probe
# =============================================================================


@dataclass(frozen=True)
class HealthProbe:
    """A ``[health_check]`` probe rendered once, before any server I/O.

    ``command`` is the exact remote command (bounded by ``min(5, timeout)``),
    ``timeout`` the overall retry window, ``display`` the redacted source
    text for user-facing output.
    """

    command: str
    timeout: int
    display: str


def build_health_probe(
    health: Any, context: Mapping[str, object]
) -> HealthProbe | None:
    """Render a ``[health_check]`` config with *context*, or ``None``.

    Unresolved placeholders raise :class:`ImproperlyConfiguredError` naming
    them (fail-closed before any server I/O). Both deploy and rollback render
    the probe through this one function so a configuration that passes one
    command passes the other.
    """
    if health is None:
        return None
    raw = health.http if health.http is not None else health.command
    assert raw is not None  # guaranteed by HealthCheckConfig
    rendered, unresolved = safe_format(raw, **context)
    if unresolved:
        raise ImproperlyConfiguredError(
            "[health_check] references unknown variables:"
            f" {', '.join(sorted(unresolved))}."
        )
    # Per-attempt bound: a hung probe must not eat the whole retry window.
    budget = min(5, health.timeout)
    if health.http is not None:
        command = f"curl -sf -o /dev/null --max-time {budget} {q(rendered)}"
    else:
        command = f"timeout {budget} bash -lc {q(rendered)}"
    return HealthProbe(
        command=command, timeout=health.timeout, display=redact(rendered)
    )


def wait_for_health_check(
    conn: Any,
    probe: HealthProbe,
    *,
    interval: float | None = None,
    clock: Any = None,
) -> tuple[bool, str]:
    """Poll *probe* until it succeeds or its window expires.

    Returns ``(ok, last_output)``; the caller owns the failure policy (deploy
    rolls back, rollback exits 6) and the diagnostic output. ``clock`` is a
    module-like object providing ``monotonic`` and ``sleep`` (tests inject a
    fake one).
    """
    interval = HEALTH_CHECK_INTERVAL if interval is None else interval
    clock = time if clock is None else clock
    deadline = clock.monotonic() + probe.timeout
    last_output = ""
    while True:
        ok, last_output = run_health_probe(conn, probe.command)
        if ok:
            return True, last_output
        if clock.monotonic() >= deadline:
            return False, last_output
        clock.sleep(interval)


def run_health_probe(conn: Any, probe: str) -> tuple[bool, str]:
    """Run *probe* once and return ``(ok, output)``.

    No retry policy here: the deploy command owns the polling window. Never
    raises. No sudo: the probe runs as the deploy user against the app's own
    listener.
    """
    out, ok = conn.run(probe, warn=True, hide=True)
    return ok, (out.strip() if out else "")
