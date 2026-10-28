"""Staging transfer, verification and release activation over SSH.

All server mutation is client-driven through :class:`SSH2Connection`. The
functions here build and run the exact remote commands and parse their
output client-side; they carry no orchestration logic of their own.

Server layout assumed (per app)::

    <app_dir>/current -> releases/<digest>       symlink, atomic swap
    <app_dir>/releases/<digest>/                 immutable, committed trees
    <app_dir>/releases/.staging-<pid>/           transfer+verify target

Staging dir naming (``releases/.staging-<pid>``) is chosen by the deploy
orchestration; every function here receives the exact path as a parameter.

Dedup: rsync's own ``--link-dest`` is unusable (RsyncProject/rsync#715);
instead the deployment orchestration hardlinks every byte-identical file
from the live release into the staging dir server-side (``serverops.
link_unchanged``) before the rsync runs, so only changed content is
transferred.

``mv -T`` (commit/swap) needs GNU coreutils; busybox mv lacks it. The
contract targets Debian/Ubuntu servers where GNU mv is standard.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fujin.errors import DeploymentError
from fujin.exit_codes import EXIT_HOST_UNREACHABLE

if TYPE_CHECKING:
    from fujin.connection import SSH2Connection

logger = logging.getLogger(__name__)

__all__ = [
    "ItemizedChange",
    "VerifyMismatch",
    "VerifyResult",
    "TransferError",
    "HostUnreachableError",
    "RsyncUnavailableError",
    "rsync_to_staging",
    "parse_itemize",
    "verify_staging",
    "cleanup_staging",
    "commit_release",
    "swap_current",
    "sweep_stale_staging",
]


class TransferError(DeploymentError):
    """Staging transfer/verify/activation failed (old release still live)."""


class HostUnreachableError(TransferError):
    """Distinct marker: the remote host could not be reached at all."""

    def __init__(self, message: str, code: int = EXIT_HOST_UNREACHABLE, **kwargs: Any):
        super().__init__(message, code=code, **kwargs)


class RsyncUnavailableError(TransferError):
    """rsync is not installed locally or on the remote host."""


# rsync exit 255, or these stderr markers, mean the host itself was
# unreachable (as opposed to a transfer-side problem).
_RSYNC_CONNECT_MARKERS = (
    "failed to connect",
    "connection refused",
    "connection timed out",
    "could not resolve hostname",
    "network is unreachable",
)

# An 11-char itemize code, a space, then the path; ``*deleting <path>`` too.
# e.g. ``>f.st...... app/main.py``, ``cd+++++++++ assets``.
_ITEMIZE_RE = re.compile(r"^(\*deleting|\S{11}) (.+)$")

_JSON_MARKER = "__FUJIN_JSON__"


@dataclass(frozen=True)
class ItemizedChange:
    """One parsed ``--itemize-changes`` record, e.g. ``>f.st...... main.py``."""

    code: str  # the 11-char code (or ``*deleting``)
    path: str

    @property
    def file_type(self) -> str:
        """``f`` file, ``d`` dir, ``L`` symlink."""
        return self.code[1]

    @property
    def created(self) -> bool:
        return "+" in self.code

    @property
    def updated(self) -> bool:
        """Attributes/content changed without being newly created."""
        return not self.created and any(c != "." for c in self.code[2:])


@dataclass(frozen=True)
class VerifyMismatch:
    expected: str
    actual: str


@dataclass(frozen=True)
class VerifyResult:
    """Result of walking a staging dir against a manifest."""

    ok: bool = True
    missing: list[str] = field(default_factory=list)
    mismatch: dict[str, VerifyMismatch] = field(default_factory=dict)
    link_mismatch: dict[str, str] = field(default_factory=dict)
    mode_mismatch: dict[str, VerifyMismatch] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.ok


def verify_report(staging: str, result: VerifyResult) -> str:
    """Full human-readable failure report for a ``VerifyResult``."""
    parts = [f"The release tree {staging} failed verification:"]
    for path in result.missing:
        parts.append(f"  missing: {path}")
    for path, mismatch in result.mismatch.items():
        parts.append(
            f"  hash mismatch: {path}"
            f" (expected {mismatch.expected}, got {mismatch.actual})"
        )
    for path, want in result.link_mismatch.items():
        parts.append(f"  symlink mismatch: {path} (expected link to {want})")
    for path, mismatch in result.mode_mismatch.items():
        parts.append(
            f"  mode mismatch: {path}"
            f" (expected {mismatch.expected}, got {mismatch.actual})"
        )
    return "\n".join(parts)


def _ssh_transport_opts(host: Any) -> str:
    opts = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "ConnectTimeout=10",
        "-p",
        str(host.port),
    ]
    if host.key_filename:
        key_path = Path(host.key_filename).expanduser().resolve()
        opts.extend(["-i", str(key_path)])
    return " ".join(opts)


def _rsync_installed_locally() -> bool:
    try:
        subprocess.run(
            ["rsync", "--version"], capture_output=True, check=False, timeout=10
        )
    except FileNotFoundError:
        return False
    return True


def _classify_rsync_failure(returncode: int, stderr: str) -> TransferError:
    lowered = stderr.lower()
    if returncode == 255 or any(m in lowered for m in _RSYNC_CONNECT_MARKERS):
        return HostUnreachableError(
            "rsync could not reach the remote host. Check SSH configuration "
            f"and that the server is up.\n{stderr.strip()}"
        )
    return TransferError(f"rsync failed with exit code {returncode}:\n{stderr.strip()}")


def rsync_to_staging(
    conn: SSH2Connection,
    local_dir: str,
    staging_dir: str,
    *,
    ignore_existing: bool = False,
    compress_zstd: bool = False,
    modes: dict[str, int] | None = None,
    exclude: set[str] | None = None,
) -> list[ItemizedChange]:
    """Rsync the assembled bundle tree into ``releases/.staging-*``.

    With ``ignore_existing`` (deploy re-ship), files the deploy pre-hardlinked
    on the server (unchanged vs the previous release, see
    ``serverops.link_unchanged``) are skipped; everything else transfers in
    full. The staging dir must already exist on the remote (the caller
    creates it next to ``releases/``). Returns the parsed itemize output.

    NOTE: we deliberately do NOT use ``--link-dest``. Rsyncs built with the
    2024 CVE fixes refuse to open a basis file whose path contains a symlink
    component (``O_NOFOLLOW`` hardening, RsyncProject/rsync#715) and, even
    with a symlink-free basis, fail whole-file verification on updated files
    (exit 23 "update discarded"). Dedup is done explicitly by
    ``serverops.link_unchanged`` instead.

    ``modes`` maps staging-relative paths to permission bits; rsync's
    ``-p`` preserves modes on transfer (the mapping documents intent).

    ``exclude`` names top-level files that are local working state (e.g.
    ``built.json``) and must not reach the release tree.
    """
    local = Path(local_dir)
    if not local.is_dir():
        raise TransferError(f"Local bundle dir missing: {local_dir}")

    if not _rsync_installed_locally():
        raise RsyncUnavailableError(
            "rsync is not installed locally. Install it, e.g. `sudo apt install rsync`."
        )

    # Pre-flight so an unbootstrapped or rsync-less server fails with an
    # actionable message before any file touches the wire.
    _, ok = conn.run("command -v rsync", warn=True, hide=True)
    if not ok:
        raise RsyncUnavailableError(
            "rsync is not installed on the remote host.\n"
            "Install it with e.g. `sudo apt install rsync`, or use a "
            "key-authenticated host."
        )

    cmd = ["rsync", "-rlptD", "--chmod=D755", "--itemize-changes"]
    for name in exclude or ():
        cmd.append(f"--exclude={name}")
    if compress_zstd:
        cmd.append("--compress-choice=zstd")
    if ignore_existing:
        cmd.append("--ignore-existing")
    cmd.extend(
        [
            "-e",
            _ssh_transport_opts(conn.host),
            f"{local}/",
            f"{conn.host.user}@{conn.host.address}:{staging_dir}",
        ]
    )

    logger.info("Rsyncing bundle into %s", staging_dir)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    except subprocess.TimeoutExpired as exc:
        raise HostUnreachableError("rsync transfer timed out.") from exc

    if result.returncode != 0:
        raise _classify_rsync_failure(result.returncode, result.stderr)

    return parse_itemize(result.stdout)


def parse_itemize(output: str) -> list[ItemizedChange]:
    """Parse rsync ``--itemize-changes`` lines from captured output."""
    changes = []
    for line in output.splitlines():
        line = line.rstrip("\r")
        m = _ITEMIZE_RE.match(line)
        if not m:
            continue
        code, path = m.group(1), m.group(2)
        if path.endswith("/"):
            path = path[:-1]
        changes.append(ItemizedChange(code=code, path=path))
    return changes


def _quote(path: str) -> str:
    return shlex.quote(path)


# Server-side verification: one python3 process, one round-trip, no
# per-file SSH chatter. The manifest is read from ``manifest.json``
# inside the staging dir (written there before verify, so no argv size
# limits come into play). Missing / non-regular / unreadable files and
# mode mismatches are recorded, never raised on, so the walk cannot hang.
_VERIFY_SCRIPT = r"""
import sys, json, hashlib, os, stat
root = sys.argv[1]
try:
    with open(os.path.join(root, "manifest.json"), "rb") as fh:
        manifest = json.load(fh)
except (OSError, ValueError) as exc:
    print("__FUJIN_JSON__" + json.dumps({"error": str(exc)}))
    sys.exit(0)
res = {"missing": [], "mismatch": {}, "link_mismatch": {}, "mode_mismatch": {}}
def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(524288), b""):
            h.update(chunk)
    return h.hexdigest()
for path, entry in manifest["entries"].items():
    full = os.path.join(root, path)
    try:
        st = os.lstat(full)
    except OSError:
        res["missing"].append(path)
        continue
    link = entry.get("link")
    if link is not None:
        try:
            ok_link = os.path.islink(full) and os.readlink(full) == link
        except OSError:
            ok_link = False
        if not ok_link:
            res["link_mismatch"][path] = link
        continue
    if not stat.S_ISREG(st.st_mode):
        res["missing"].append(path)
        continue
    want_mode = int(entry["mode"], 8)
    got_mode = stat.S_IMODE(st.st_mode)
    if got_mode != want_mode:
        res["mode_mismatch"][path] = [entry["mode"], format(got_mode, "04o")]
    try:
        got = digest(full)
    except OSError as exc:
        res["mismatch"][path] = "unreadable (errno %s)" % exc.errno
        continue
    want = entry["sha256"]
    if got != want:
        res["mismatch"][path] = got
print("__FUJIN_JSON__" + json.dumps(res))
"""


def verify_staging(
    conn: SSH2Connection, staging_dir: str, manifest_dict: dict[str, Any]
) -> VerifyResult:
    """Verify the staging tree byte-for-byte against a manifest.

    ``manifest_dict`` is a :func:`fujin.manifest.manifest_to_dict` payload
    (entries keyed by staging-relative path under ``entries``). The remote
    script reads ``manifest.json`` from the staging dir (written there
    before verify), so no oversized argv is involved. Runs a single
    python3 one-shot remotely and parses the JSON it prints.
    """
    command = (
        f"python3 - {shlex.quote(staging_dir)} "
        f"<<'_FUJIN_PY'\n{_VERIFY_SCRIPT}\n_FUJIN_PY"
    )

    out, ok = conn.run(command, warn=True, hide=True)
    if not ok:
        raise TransferError(
            f"Staging verification script failed to run:\n{out.strip()}"
        )
    try:
        payload = next(
            line for line in out.splitlines() if line.startswith(_JSON_MARKER)
        )[len(_JSON_MARKER) :]
        result = json.loads(payload)
    except (StopIteration, ValueError) as exc:
        raise TransferError(
            f"Could not parse verification output: {out.strip()[:400]}"
        ) from exc

    if result.get("error"):
        raise TransferError(
            f"Staging verification could not read the manifest in "
            f"{staging_dir}: {result['error']}"
        )

    return VerifyResult(
        ok=not (
            result["missing"]
            or result["mismatch"]
            or result["link_mismatch"]
            or result["mode_mismatch"]
        ),
        missing=result["missing"],
        mismatch={
            path: VerifyMismatch(
                expected=manifest_dict["entries"][path]["sha256"], actual=actual
            )
            for path, actual in result["mismatch"].items()
        },
        link_mismatch=result["link_mismatch"],
        mode_mismatch={
            path: VerifyMismatch(expected=want, actual=got)
            for path, (want, got) in result["mode_mismatch"].items()
        },
    )


def cleanup_staging(conn: SSH2Connection, staging_dir: str) -> None:
    """Remove the staging tree (after a verify failure or a commit)."""
    conn.run(f"rm -rf {_quote(staging_dir)}", warn=True, hide=True)


def commit_release(conn: SSH2Connection, staging_dir: str, digest: str) -> None:
    """Commit a verified staging dir as ``releases/<digest>/``.

    Refuses to overwrite an existing release with the same digest name.
    The rename uses ``mv -T`` (GNU coreutils), which atomically errors on
    an existing destination directory instead of silently nesting.
    """
    # dest sits next to the staging dir:  <app>/releases/.staging-X ->
    # <app>/releases/<digest>. Falls back to a plain relative path if
    # staging_dir carries no directory component.
    parent, _, _ = staging_dir.rpartition("/")
    dest = f"{parent}/{digest}" if parent else f"releases/{digest}"
    cmd = (
        f"if [ -e {dest} ]; then echo {_JSON_MARKER}EXISTS; exit 3; fi && "
        f"mv -T {_quote(staging_dir)} {dest}"
    )
    out, ok = conn.run(cmd, warn=True, hide=True)
    if not ok:
        if _JSON_MARKER + "EXISTS" in out:
            raise TransferError(
                f"Release {dest} already exists; refusing to overwrite it."
            )
        raise TransferError(f"Failed to commit release:\n{out.strip()}")


def swap_current(conn: SSH2Connection, app_dir: str, digest: str) -> None:
    """Atomically point ``current`` at ``releases/<digest>``.

    ``ln -s`` then ``mv -T`` is the atomic point-of-commit. ``current``
    never dangles (the target release was committed just before), and a
    failed attempt leaves no stray ``current.next`` behind.
    """
    quoted_app = _quote(app_dir)
    cmd = (
        f"rm -f {quoted_app}/current.next && "
        f"ln -s releases/{digest} {quoted_app}/current.next && "
        f"mv -T {quoted_app}/current.next {quoted_app}/current"
    )
    _, ok = conn.run(cmd, warn=True, hide=True)
    if not ok:
        conn.run(f"rm -f {quoted_app}/current.next", warn=True, hide=True)
        raise TransferError(
            f"Failed to atomically swap {app_dir}/current -> releases/{digest}."
        )


def sweep_stale_staging(conn: SSH2Connection, app_dir: str) -> list[str]:
    """Delete orphaned ``.staging-*`` dirs left by crashed deploys.

    Returns the removed dir basenames.
    """
    releases_dir = f"{app_dir}/releases"
    cmd = (
        f"find {_quote(releases_dir)} -maxdepth 1 -type d "
        f"-name '.staging-*' -print -prune 2>/dev/null || true"
    )
    out, _ = conn.run(cmd, warn=True, hide=True)
    found = [line.strip() for line in out.splitlines() if line.strip()]
    for name in found:
        conn.run(f"rm -rf {_quote(name)}", warn=True, hide=True)
    return found
