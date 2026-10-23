"""Client-driven server-side operations for the release-tree store.

Everything here mutates the server through :class:`SSH2Connection.run` /
:meth:`~SSH2Connection.put`, reusing the sudo/pty conventions of
``commands/server.py`` and the ``_installer.py`` phase logic. Pure
decision-making lives in ``store.py``; this module is the I/O port layer.
"""

from __future__ import annotations

import base64
import json
import secrets
import logging
import shlex
import msgspec
from collections.abc import Mapping, Sequence
from typing import Any

from fujin import manifest as m
from fujin.store import State, parse_state, serialize_state

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


def _q(*parts: str) -> str:
    return " ".join(shlex.quote(p) for p in parts)


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
    """Release a lock acquired by :func:`acquire_lock`."""
    if lock.startswith("flock:"):
        pid = lock.removeprefix("flock:")
        conn.run(f"kill {shlex.quote(pid)} 2>/dev/null; true", warn=True, hide=True)
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


# =============================================================================
# Code install (into staging; wave: deploy step 4, replaces _installer PHASE 2)
# =============================================================================


def install_code_into_staging(
    conn: Any,
    staging_dir: str,
    installation_mode: str,
    python_version: str | None,
    requirements: str | None,
    dist_local_path: str,
    *,
    app_name: str,
) -> None:
    """Install the application code into *staging*, never into a live venv.

    - ``python-package``: upload the distfile, build ``uv venv`` at
      ``{staging}/venv`` and ``uv pip install`` it there. The transient
      distfile is removed afterwards so the committed tree stays manifest-clean.
    - ``binary``: upload the distfile to ``{staging}/bin/{app_name}``,
      chmod 755.

    ``requirements`` is a local requirements.txt path, uploaded to
    ``{staging}/requirements.txt`` (it is committed as an asset entry).
    """
    staging_q = shlex.quote(staging_dir)

    if installation_mode == "python-package":
        if not python_version:
            raise ServerOpsError("python mode requires a python_version")
        distfile_remote = f"{staging_dir}/.distfile"
        conn.put(dist_local_path, distfile_remote)
        _, ok = conn.run(
            f"UV_PYTHON_INSTALL_DIR={UV_PYTHON_INSTALL_DIR} "
            f"uv venv -p {shlex.quote(python_version)} --managed-python "
            f"{staging_q}/{_PYTHON_ROOT}",
            warn=True,
        )
        if not ok:
            raise ServerOpsError(f"uv venv creation failed in {staging_dir}")
        pip_cmd = (
            f"UV_PYTHON_INSTALL_DIR={UV_PYTHON_INSTALL_DIR} UV_COMPILE_BYTECODE=1 "
            f"uv pip install --python {staging_q}/{_PYTHON_ROOT}/bin/python "
            f"{staging_q}/.distfile --no-deps"
        )
        if requirements:
            conn.put(requirements, f"{staging_dir}/requirements.txt")
            pip_cmd += f" -r {staging_q}/requirements.txt"
        _, ok = conn.run(pip_cmd, warn=True)
        if not ok:
            raise ServerOpsError("uv pip install into staging venv failed")
        # Group-readable/executable venv, owner-writable (installer PHASE 2
        # permission model), applied BEFORE hashing so recorded modes match.
        _, ok = conn.run(
            f"chmod -R u+rwX,go+rX {staging_q}/{_PYTHON_ROOT}"
            f" && chmod -R u+x {staging_q}/{_PYTHON_ROOT}/bin",
            warn=True,
        )
        if not ok:
            raise ServerOpsError(f"Failed to set venv permissions in {staging_dir}")
        conn.run(f"rm -f {staging_q}/.distfile", warn=True, hide=True)
        return

    if installation_mode == "binary":
        conn.run(f"mkdir -p {staging_q}/{_BINARY_ROOT}", warn=True, hide=True)
        conn.put(dist_local_path, f"{staging_dir}/{_BINARY_ROOT}/{app_name}")
        conn.run(
            f"chmod 755 {staging_q}/{_BINARY_ROOT}/{shlex.quote(app_name)}",
            warn=True,
        )
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


# =============================================================================
# Systemd units (adapted _installer PHASE 3, client-driven)
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
    valid = set(unit_files) | set(dropins)
    valid |= {f"{unit}.d" for unit in valid}

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
    """Disable + delete installed unit files/dropin dirs no longer declared."""
    # Quote the prefix only; the glob must stay outside the quotes so the
    # remote shell expands it. No match -> empty output, nothing stale.
    out, _ok = conn.run(
        f"ls -1 /etc/systemd/system/{shlex.quote(app_name)}* 2>/dev/null || true",
        warn=True,
        hide=True,
    )
    dir_prefix = "/etc/systemd/system/"
    installed = [
        line.strip().removeprefix(dir_prefix)
        for line in out.splitlines()
        if line.strip()
    ]
    stale = [name for name in installed if name not in valid]
    if not stale:
        return
    regular = [u for u in stale if not u.endswith("@.service")]
    template = [u for u in stale if u.endswith("@.service")]
    if regular:
        conn.run(
            f"sudo systemctl disable --now --quiet {_q(*regular)}", warn=True, hide=True
        )
    if template:
        conn.run(
            f"sudo systemctl disable --quiet {_q(*template)}", warn=True, hide=True
        )
    conn.run(f"sudo systemctl reset-failed {_q(*stale)}", warn=True, hide=True)
    targets = " ".join(shlex.quote(f"/etc/systemd/system/{name}") for name in stale)
    conn.run(f"sudo rm -rf {targets}", warn=True, hide=True)


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


def read_current_manifest(conn: Any, app_dir: str) -> m.Manifest | None:
    """Decode the manifest of the live release; None when absent."""
    out, ok = conn.run(
        f"cat {shlex.quote(f'{app_dir}/current/{MANIFEST_PATH}')}",
        warn=True,
        hide=True,
    )
    if not ok:
        return None
    return m.decode_manifest(out)


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
# Thin enable/start & stop/disable wrappers (convergence callsites)
# =============================================================================


def enable_and_start(conn: Any, units: Sequence[str]) -> bool:
    """Enable and start units (``sudo systemctl enable --now``). No-op on
    empty input."""
    if not units:
        return True
    _, ok = conn.run(f"sudo systemctl enable --now {_q(*units)}", warn=True)
    return ok


def stop_and_disable(conn: Any, units: Sequence[str]) -> bool:
    """Stop and disable units (``systemctl disable --now``). No-op on empty
    input; template instances converge through the same command."""
    if not units:
        return True
    _, ok = conn.run(f"sudo systemctl disable --now {_q(*units)}", warn=True)
    if ok:
        conn.run(f"sudo systemctl reset-failed {_q(*units)}", warn=True, hide=True)
    return ok
