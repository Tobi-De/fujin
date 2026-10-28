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
import secrets
import logging
import shlex
import msgspec
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

import cappa

from fujin import manifest as m
from fujin.store import (
    State,
    convergence,
    gc_plan,
    instance_sort_key,
    parse_state,
    serialize_state,
)

logger = logging.getLogger(__name__)

UV_PYTHON_INSTALL_DIR = "/opt/fujin/.python"
#: Holder TTL must outlive the longest deploy phase (the rsync transfer alone
#: allows up to 3600s); renewal is done implicitly by releasing and re-taking.
LOCK_TTL = 7200
LOCK_PATH = "store/lock"
LEGACY_LOCK_PATH = ".deploy_lock"
STATE_PATH = "store/state.json"
MANIFEST_PATH = "manifest.json"
ENV_PATH = ".env"

_PYTHON_ROOT = "venv"
_BINARY_ROOT = "bin"


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
    for digest in kill:
        conn.run(
            f"rm -rf {shlex.quote(f'{app_dir}/releases/{digest}')}",
            warn=True,
            hide=True,
        )
    if kill:
        output.info(f"Garbage-collected {len(kill)} old release(s).")
    return kill


def _b64(content: bytes | str) -> str:
    if isinstance(content, str):
        content = content.encode("utf-8")
    return base64.b64encode(content).decode("ascii")


def _python_script(body: str, marker: str = "FUJINSCRIPT") -> str:
    """Wrap an inline python program for execution over ``conn.run``."""
    return f"python3 - <<'{marker}'\n{body}\n{marker}"


def _sudo_write(
    conn: Any, path: str, content: bytes | str, *, chown: str | None = None
) -> None:
    """Write file content to a root-owned path via base64 stream + mv."""
    path_q = shlex.quote(path)
    tmp_q = shlex.quote(f"{path}.fujin-new")
    b64 = _b64(content)
    cmd = f"echo {b64} | base64 -d | sudo tee {tmp_q} >/dev/null && sudo mv {tmp_q} {path_q}"
    if chown is not None:
        cmd += f" && sudo chown {chown} {path_q}"
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

    Old ``.install/`` and ``.versions/`` directories are left untouched.
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

    Non-fatal to no-op when the user already exists; raises when the user
    is still missing afterwards (mirrors the retired installer PHASE 1 and
    ``server bootstrap``'s user creation).
    """
    cmd = (
        f"sudo groupadd -f {shlex.quote(app_user)} && "
        "sudo useradd --system --no-create-home --shell /usr/sbin/nologin "
        f"-g {shlex.quote(app_user)} {shlex.quote(app_user)} || true"
    )
    conn.run(cmd, warn=True, hide=True)
    _, exists = conn.run(f"id -u {shlex.quote(app_user)}", warn=True, hide=True)
    if not exists:
        raise ServerOpsError(f"Failed to create app user {app_user}")


# =============================================================================
# Deploy lock
# =============================================================================


def acquire_lock(conn: Any, app_dir: str, *, ttl: int = LOCK_TTL) -> str:
    """Take the deploy lock, failing loudly if another deployment holds it.

    Two belts:

    1. ``flock`` on ``store/lock`` via a detached holder process that keeps
       the lock for ``ttl`` seconds (preferred, works across shells). The
       holder writes its pid to ``store/lock.holder`` once it owns the lock,
       which lets a crashed deployer's stale marker be detected and stolen.
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
            # Fully detached from the SSH exec session: setsid + nohup +
            # </dev/null. A partially detached background child would keep
            # the SSH channel from reaching EOF and make conn.run hang.
            # The holder writes ITS OWN pid + a random token (setsid may
            # fork, so the shell's $! is unreliable) as the single truth.
            token = secrets.token_hex(8)
            holder_cmd = (
                f"FUJIN_LOCK_TOKEN={token} setsid nohup bash -c "
                f"'echo $$-$FUJIN_LOCK_TOKEN > {holder_q}; "
                f"exec 9>{lock_q}; flock -x 9 && sleep {ttl}'"
                f" >/dev/null 2>&1 & echo $!"
            )
            out, ok = conn.run(holder_cmd, warn=True, hide=True)
            holder_pid = out.strip() if ok else ""

            probe, _ = conn.run(
                f"sleep 0.5; "
                f"if flock -n {lock_q} true 2>/dev/null; then echo FREE; "
                f"else echo HELD-$(cat {holder_q} 2>/dev/null || echo ?); fi; "
                f"pid=$(cat {holder_q} 2>/dev/null | cut -d- -f1); "
                f'if [ -n "$pid" ] && [ "$pid" != "?" ] && kill -0 "$pid" 2>/dev/null; '
                f'then echo ALIVE-"$pid"; else echo DEAD; fi',
                warn=True,
                hide=True,
            )
            lines = [line.strip() for line in probe.splitlines() if line.strip()]
            held_line = next((l for l in lines if l.startswith("HELD-")), None)
            alive = next((l for l in lines if l.startswith("ALIVE-")), None)

            # The holder file ("pid-token") is the authority: setsid may
            # fork, so the shell's $! is not the holder pid.
            holder_file = held_line.removeprefix("HELD-") if held_line else None
            if holder_file == "?":
                holder_file = None
            file_pid = None
            file_token = None
            if holder_file:
                file_pid, _, file_token = holder_file.partition("-")

            if file_pid and alive == f"ALIVE-{file_pid}":
                if file_token == token:
                    return f"flock:{file_pid}"
                # A live holder with a different token: never switch belts.
                raise ServerOpsError(
                    "Another deployment is in progress on the server\n"
                    f"(flock holder pid {file_pid}). Wait for it to finish."
                )

            # Stale state (holder crashed, lock free after crash, or our own
            # holder failed to acquire): clean up and retry once.
            dead_pid, _, _ = (holder_file or "").partition("-")
            conn.run(
                f"kill {shlex.quote(holder_pid)} 2>/dev/null; "
                f"kill {shlex.quote(dead_pid or '')} 2>/dev/null; true",
                warn=True,
                hide=True,
            )
            conn.run(f"rm -f {holder_q}", warn=True, hide=True)
            if held_line is None:
                # Lock is genuinely free but flock could not hold it here
                # (unsupported filesystem, ...): the noclobber belt is safe.
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
        conn.run(
            f"kill -- -{shlex.quote(pid)} 2>/dev/null;"
            f" kill {shlex.quote(pid)} 2>/dev/null; true",
            warn=True,
            hide=True,
        )
        conn.run(
            f"rm -f {shlex.quote(f'{app_dir}/{LOCK_PATH}.holder')}",
            warn=True,
            hide=True,
        )
    else:
        path = lock.removeprefix("file:")
        conn.run(f"rm -f {shlex.quote(path)}", warn=True, hide=True)


def parse_holder_pid(output: str) -> str | None:
    """Parse the holder pid from an acquire_lock probe line, or None if free."""
    if output.startswith("HELD-"):
        value = output.removeprefix("HELD-")
        if value and value != "?":
            return value
    return None


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


def install_code_into_staging(
    conn: Any,
    staging_dir: str,
    installation_mode: str,
    python_version: str | None,
    requirements: str | None,
    dist_tree_path: str,
    *,
    app_name: str,
) -> None:
    """Install the application code into *staging*, never into a live venv.

    - ``python-package``: the dist file is already part of the staging tree
      (rsynced to ``{staging}/{dist_tree_path}``); ``uv venv`` builds at
      ``{staging}/venv`` and ``uv pip install`` installs it there. The staged
      dist file stays in the tree as a ``code`` entry (it IS the code).
    - ``binary``: the dist file is staged as ``{staging}/bin/{app_name}``
      (rsynced); this only ensures the directory exists and the file is 755.

    ``requirements`` is a local requirements.txt path, uploaded to
    ``{staging}/requirements.txt`` (it is committed as an asset entry).
    """
    staging_q = shlex.quote(staging_dir)
    dist_in_tree = f"{staging_q}/{shlex.quote(dist_tree_path)}"

    if installation_mode == "python-package":
        if not python_version:
            raise ServerOpsError("python mode requires a python_version")
        _, ok = conn.run(
            f"UV_PYTHON_INSTALL_DIR={UV_PYTHON_INSTALL_DIR} "
            f"uv venv --relocatable -p {shlex.quote(python_version)} --managed-python "
            f"{staging_q}/{_PYTHON_ROOT}",
            warn=True,
        )
        if not ok:
            raise ServerOpsError(f"uv venv creation failed in {staging_dir}")
        pip_cmd = (
            f"UV_PYTHON_INSTALL_DIR={UV_PYTHON_INSTALL_DIR} UV_COMPILE_BYTECODE=1 "
            f"uv pip install --python {staging_q}/{_PYTHON_ROOT}/bin/python "
            f"{dist_in_tree} --no-deps"
        )
        if requirements:
            conn.put(requirements, f"{staging_dir}/requirements.txt")
            pip_cmd += f" -r {staging_q}/requirements.txt"
        _pip_out, ok = conn.run(pip_cmd, warn=True)
        if not ok:
            raise ServerOpsError(
                f"uv pip install into staging venv failed:\n{_pip_out.strip()}"
            )
        # Group-readable/executable venv, owner-writable (installer PHASE 2
        # permission model), applied BEFORE hashing so recorded modes match.
        _, ok = conn.run(
            f"chmod -R u+rwX,go+rX {staging_q}/{_PYTHON_ROOT}"
            f" && chmod -R u+x {staging_q}/{_PYTHON_ROOT}/bin",
            warn=True,
        )
        if not ok:
            raise ServerOpsError(f"Failed to set venv permissions in {staging_dir}")
        return

    if installation_mode == "binary":
        conn.run(f"mkdir -p {staging_q}/{_BINARY_ROOT}", warn=True, hide=True)
        _, ok = conn.run(f"chmod 755 {dist_in_tree}", warn=True)
        if not ok:
            raise ServerOpsError(f"Failed to chmod dist file in {staging_dir}")
        return

    raise ServerOpsError(f"Unknown installation mode: {installation_mode}")


def hash_server_built(
    conn: Any, staging_dir: str, installation_mode: str
) -> dict[str, dict[str, Any]]:
    """Hash server-built entries (``venv/**`` or ``bin/**``) on the remote host.

    Returns ``{path: {"sha256": hex, "mode": int}}`` for regular files and
    ``{path: {"link": target, "mode": int}}`` for symlinks, keyed by
    staging-relative paths. Raises on a non-zero exit or undecodable output.
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
            "result = {}",
            "for dirpath, dirnames, filenames in os.walk(root):",
            "    for name in dirnames + filenames:",
            "        path = os.path.join(dirpath, name)",
            "        rel = os.path.relpath(path, os.path.dirname(root))",
            "        if os.path.islink(path):",
            "            result[rel] = {'link': os.readlink(path), 'mode': 0o777}",
            "        elif os.path.isfile(path):",
            "            digest = hashlib.sha256()",
            "            with open(path, 'rb') as fh:",
            "                for chunk in iter(lambda: fh.read(131072), b''):",
            "                    digest.update(chunk)",
            "            result[rel] = {'sha256': digest.hexdigest(),"
            " 'mode': stat.S_IMODE(os.lstat(path).st_mode)}",
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


def normalize_staging_ownership(conn: Any, staging_dir: str, app_user: str) -> None:
    """Force the staged tree into the deploy permission model.

    Files hardlinked from the previous release (``link_unchanged``) share
    their inode with the live tree, so a blindly bit-adding ``chmod -R`` would
    mutate the live release (immutability violation). chgrp covers them (the live
    release's group is already ``{app_user}``) and chmod is applied only to
    directories and to files private to the staging tree.
    """
    staging_q = shlex.quote(staging_dir)
    _, ok = conn.run(
        f"sudo chgrp -R {shlex.quote(app_user)} {staging_q} && "
        # Directories: group-traversable, and setgid so late writes (hooks,
        # runtime) keep inheriting the group.
        f"sudo find {staging_q} -type d -exec chmod g+rxs {{}} + && "
        # Non-hardlinked files only.
        f"sudo find {staging_q} -links 1 -type f -exec chmod g+rX {{}} +",
        warn=True,
    )
    if not ok:
        raise ServerOpsError(f"Failed to normalize ownership of {staging_dir}")


# =============================================================================
# Systemd units
# =============================================================================


def install_units(
    conn: Any,
    app_name: str,
    unit_files: Mapping[str, str],
    dropins: Mapping[str, Mapping[str, str]] | None = None,
) -> None:
    """Install unit files (service/socket/timer) + dropins into
    ``/etc/systemd/system``, remove stale units of this app, and
    ``daemon-reload``.

    ``unit_files`` maps the unit file name (e.g. ``myapp-web@.service``) to
    its rendered content. ``dropins`` maps a unit name to
    ``{dropin-filename: content}``; the caller expands ``common.d`` per unit.
    """
    dropins = dropins or {}
    units = set(unit_files) | set(dropins)
    valid = units | {f"{unit}.d" for unit in units}
    # Dropin *files* are valid too: kept ``*.d/`` dirs must not accumulate
    # conf files from an older release (pruned at depth 2 below).
    valid |= {f"{unit}.d/{name}" for unit, files in dropins.items() for name in files}

    _remove_stale_units(conn, app_name, valid)

    for name, content in unit_files.items():
        _sudo_write(conn, f"/etc/systemd/system/{name}", content)
    for unit, files in dropins.items():
        dir_path = f"/etc/systemd/system/{unit}.d"
        for name, content in files.items():
            _sudo_write(conn, f"{dir_path}/{name}", content)

    _, ok = conn.run("sudo systemctl daemon-reload", warn=True)
    if not ok:
        raise ServerOpsError("systemctl daemon-reload failed")


def _remove_stale_units(conn: Any, app_name: str, valid: set[str]) -> None:
    """Disable + delete installed units and dropin files no longer declared.

    Covers both depth 1 (``app-*.service``, ``app-*.socket``, ``app-*.timer``,
    ``app-*.d`` dirs) and depth 2 (files inside a kept ``app-*.d`` dir). A
    stale dropin file must go even when its directory remains in use.
    """
    # Quote the prefix pieces only; the globs must stay outside the quotes so
    # the remote shell expands them. No match -> empty output, nothing stale.
    out, _ok = conn.run(
        f"cd /etc/systemd/system && find . -maxdepth 2 \\( "
        f"-name {shlex.quote(app_name + '*')} "
        f"-o -path {shlex.quote(f'./{app_name}*.d/*')} "
        f"\\) -print 2>/dev/null | sort",
        warn=True,
        hide=True,
    )
    installed = [
        line.strip().removeprefix("./") for line in out.splitlines() if line.strip()
    ]
    stale = [name for name in installed if name not in valid]
    if not stale:
        return
    # Only unit names can be disabled; dropin files/dirs are just removed.
    stale_units = [name for name in stale if "/" not in name]
    regular = [u for u in stale_units if not u.endswith("@.service")]
    template = [u for u in stale_units if u.endswith("@.service")]
    if regular:
        conn.run(
            f"sudo systemctl disable --now --quiet {q(*regular)}", warn=True, hide=True
        )
    if template:
        conn.run(f"sudo systemctl disable --quiet {q(*template)}", warn=True, hide=True)
    if stale_units:
        conn.run(f"sudo systemctl reset-failed {q(*stale_units)}", warn=True, hide=True)
    targets = " ".join(shlex.quote(f"/etc/systemd/system/{name}") for name in stale)
    conn.run(f"sudo rm -rf {targets}", warn=True, hide=True)


def installed_unit_tree(conn: Any, app_name: str) -> dict[str, str | None]:
    """Read the units + dropins installed for *app_name* (read-only).

    Returns ``{relative path: content}``; directory entries (dropin dirs)
    map to ``None``. Paths are relative to ``/etc/systemd/system``. An
    unreadable file also maps to ``None`` (it is then reported as drifted by
    the caller, never silently ignored).
    """
    parent = "/etc/systemd/system"
    pattern = f"{app_name}*"
    dropin_glob = f"./{app_name}*.d/*"
    cmd = (
        f"cd {shlex.quote(parent)} 2>/dev/null && "
        f"find . -maxdepth 2 \\( -name {shlex.quote(pattern)}"
        f" -o -path {shlex.quote(dropin_glob)} \\) -print 2>/dev/null | sort"
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
    # main write. Restore the backup before touching anything else.
    if bak_exists and not main_exists:
        conn.run(
            f"sudo cp {bak_q} {path_q} && sudo chown caddy:caddy {path_q}",
            warn=True,
            hide=True,
        )
        main_exists = True

    old_content: str | None = None
    if main_exists:
        out, ok = conn.run(f"cat {path_q}", warn=True, hide=True)
        old_content = out if ok else None

    if old_content is not None:
        _sudo_write(conn, bak_path, old_content, chown="caddy:caddy")

    _sudo_write(conn, caddy_config_path, content, chown="caddy:caddy")

    _, ok = conn.run("sudo systemctl reload caddy", warn=True)
    if ok:
        return True

    logger.warning("Caddy reload failed; restoring previous configuration")
    if old_content is not None:
        _sudo_write(conn, caddy_config_path, old_content, chown="caddy:caddy")
    elif bak_exists:
        conn.run(f"sudo cp {bak_q} {path_q}", warn=True, hide=True)
    else:
        conn.run(f"sudo rm -f {path_q}", warn=True, hide=True)
    return False


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
# Release-file permission fixups
# =============================================================================


def set_envfile_mode(conn: Any, release_dir: str) -> None:
    """Enforce ``.env`` mode 0640 in a release or staging dir.

    Ownership needs no sudo: the file is created by ``deploy_user`` inside a
    setgid ``2755`` directory, so it already inherits the ``app_user`` group.
    """
    _, ok = conn.run(
        f"chmod 0640 {shlex.quote(f'{release_dir}/{ENV_PATH}')}", warn=True, hide=True
    )
    if not ok:
        raise ServerOpsError(f"Failed to chmod .env in {release_dir}")


# =============================================================================
# Template-instance convergence plan
# =============================================================================


def _list_template_instances(conn: Any, command: str, pattern: str) -> set[str]:
    """Instance unit names reported by a ``systemctl`` listing (non-fatal).

    Both the enabled and the active view are queried: enablement alone misses
    an instance somebody stopped by hand, runtime state alone misses an
    enabled instance that is not running. Bare templates are dropped.
    """
    out, _ok = conn.run(
        f"{command} {shlex.quote(pattern)} 2>/dev/null || true",
        warn=True,
        hide=True,
    )
    instances: set[str] = set()
    for line in out.splitlines():
        parts = line.split()
        if not parts:
            continue
        name = parts[0]
        if not name.endswith(".service") or name.endswith("@.service"):
            continue
        instances.add(name)
    return instances


def template_instance_plan(
    conn: Any, app_name: str, desired: Iterable[str]
) -> tuple[list[str], list[str]]:
    """Converge plan for template instances of *app_name*.

    *desired* holds instance ids (``svc@1``) or unit names
    (``svc@1.service``). Returns ``(start_specs, stop_specs)`` as full unit
    names: ``start_specs`` sorted ascending covers instances that are missing,
    stopped-but-enabled or running-but-disabled; ``stop_specs`` are the
    present instances not desired, highest instance number first.
    """
    pattern = f"{app_name}-*@*.service"
    enabled = _list_template_instances(
        conn, "systemctl list-unit-files --no-legend", pattern
    )
    active = _list_template_instances(
        conn,
        "systemctl list-units --type=service --state=active --no-legend",
        pattern,
    )

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
# Post-start health probe
# =============================================================================


def run_health_probe(conn: Any, probe: str) -> tuple[bool, str]:
    """Run *probe* once and return ``(ok, output)``.

    No retry policy here: the deploy command owns the polling window. Never
    raises. No sudo: the probe runs as the deploy user against the app's own
    listener.
    """
    out, ok = conn.run(probe, warn=True, hide=True)
    return ok, (out.strip() if out else "")
