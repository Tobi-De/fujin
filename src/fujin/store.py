"""Release-tree store: pure state machine for deploys, rollbacks, GC.

No I/O here except the tiny ``write_state`` helper, which the command layer
calls with either a path or an open file object. No SSH types, no subprocess.

See the Tier-1 contract, fujin #77/#79 #76, and the bundle/store design
notes in the linked issues.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Protocol

import msgspec


class SupportsWrite(Protocol):
    """Anything accepting ``write(bytes)`` (file objects, SSH streams)."""

    def write(self, data: bytes) -> object: ...


__all__ = [
    "Step",
    "ACTIVATION_STEPS",
    "ROLLBACK_STEPS",
    "DeployRecord",
    "State",
    "parse_state",
    "serialize_state",
    "write_state",
    "record_deploy",
    "record_rollback",
    "activation_plan",
    "rollback_plan",
    "restart_set",
    "unit_entry_name",
    "desired_instances",
    "instance_sort_key",
    "convergence",
    "gc_plan",
    "is_noop",
]

# Digests are stored as hex strings; a "sha256:<hex>" form is accepted on
# input for lenient comparison against kaze-style state files.
_SHA256_PREFIX = "sha256:"


def normalize_digest(digest: str) -> str:
    """Strip an optional ``sha256:`` prefix so digests compare consistently."""
    if digest.startswith(_SHA256_PREFIX):
        return digest[len(_SHA256_PREFIX) :]
    return digest


class ManifestLike(Protocol):
    """Minimal structural view of a manifest (owned by manifest.py).

    ``entries`` maps path -> entry; entries expose ``kind`` and hashable
    content so equal tuples mean equal bytes.
    """

    @property
    def entries(self) -> Mapping[str, Any]: ...

    @property
    def dist_sha256(self) -> str: ...


# --------------------------------------------------------------------------
# State schema (store/state.json)
# --------------------------------------------------------------------------


class DeployRecord(msgspec.Struct, kw_only=True):
    manifest: str
    at: str  # RFC3339 timestamp, informational; never part of a digest
    result: str  # "ok" | "rollback" | ...


class State(msgspec.Struct, kw_only=True):
    """Generations registry. ``current`` mirrors the ``current`` symlink."""

    current: str | None = None
    previous: list[str] = msgspec.field(
        default_factory=list
    )  # newest first, v1 keeps exactly one
    units_installed_from: str | None = None
    deploys: list[DeployRecord] = msgspec.field(default_factory=list)
    #: client-side pre-merge digest (what the deploy computes before
    #: transfer); no-op detection (#78) compares against this, while
    #: ``current`` stays the final post-merge digest (symlink/releases
    #: naming truth).
    client_digest: str | None = None


def parse_state(data: bytes | str) -> State:
    """Parse state.json content, tolerating ``sha256:``-prefixed digests."""
    state = msgspec.json.decode(data, type=State)
    state.current = normalize_digest(state.current) if state.current else None
    state.previous = [normalize_digest(d) for d in state.previous]
    state.units_installed_from = (
        normalize_digest(state.units_installed_from)
        if state.units_installed_from
        else None
    )
    state.client_digest = (
        normalize_digest(state.client_digest) if state.client_digest else None
    )
    state.deploys = [normalize_deploys(d) for d in state.deploys]
    return state


def normalize_deploys(record: DeployRecord) -> DeployRecord:
    record.manifest = normalize_digest(record.manifest)
    return record


def serialize_state(state: State) -> bytes:
    """Canonical bytes of state.json (compact JSON, insertion-key order)."""
    return msgspec.json.encode(state)


def write_state(target: Path | SupportsWrite, state: State) -> None:
    """Persist state, atomically for a path (``.tmp`` + rename).

    Also accepts an open file object (anything with ``write(bytes)``) so the
    SSH command layer can stream it without a local filesystem.
    """
    data = serialize_state(state)
    if hasattr(target, "write"):
        target.write(data)
        return
    path = Path(target)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def record_deploy(
    state: State,
    digest: str,
    at: str,
    client_digest: str | None = None,
    result: str = "ok",
) -> State:
    """Pure state transition after a successful (or rolled-back) deploy.

    ``digest`` is the final post-merge digest (symlink/releases naming
    truth); ``client_digest`` is the client-side pre-merge digest the next
    deploy's no-op check (#78) compares against. A deploy makes
    ``previous`` hold exactly the pre-deploy current (single-level rollback
    history, v1). Pure: returns a new ``State``.
    """
    digest = normalize_digest(digest)
    client = normalize_digest(client_digest) if client_digest else digest
    old_current = normalize_digest(state.current) if state.current else None
    previous = [old_current] if old_current and old_current != digest else []
    return State(
        current=digest,
        client_digest=client,
        previous=previous,
        units_installed_from=digest,
        deploys=[
            *state.deploys,
            DeployRecord(manifest=digest, at=at, result=result),
        ],
    )


def record_rollback(state: State, target: str, at: str) -> State:
    """Pure state transition after a rollback to ``target``.

    Rollback is treated as a deploy of the old release in reverse: current
    becomes the target and ``previous`` holds the pre-rollback current, so
    deploys history records ``result="rollback"`` and the swap remains a
    single level (no recursion into previous-of-previous).

    ``client_digest`` is cleared (not set to ``target``): the target was not
    transferred by the client, so recording it as the last-known client
    digest would permanently break #78 no-op detection. ``None`` forces the
    next deploy to transfer; identical content is then gracefully reused
    (deploy checks for an already-committed release dir).
    """
    target = normalize_digest(target)
    old_current = normalize_digest(state.current) if state.current else None
    return State(
        current=target,
        client_digest=None,
        previous=[old_current] if old_current and old_current != target else [],
        units_installed_from=target,
        deploys=[
            *state.deploys,
            DeployRecord(manifest=target, at=at, result="rollback"),
        ],
    )


# --------------------------------------------------------------------------
# Activation step table
# --------------------------------------------------------------------------


class Step(msgspec.Struct, frozen=True):
    """Ordered activation step with its failure semantics."""

    name: str
    on_failure: str  # "abort" (old release still live) | "rollback" (swap committed)


#: Deploy steps in execution order. Steps 1–9 run before the swap commits, so
#: failure aborts with the old release still live; after the swap the current
#: symlink already points at the new tree, so failure triggers auto-rollback.
ACTIVATION_STEPS: tuple[Step, ...] = (
    Step("ensure", "abort"),  # idempotent root setup (runs on every deploy)
    Step("prepare", "abort"),  # build/render/manifest/no-op decision
    Step("transfer", "abort"),  # rsync staging tree
    Step("install", "abort"),  # server-side code install into staging
    Step("verify", "abort"),  # staging tree vs manifest
    Step("commit", "abort"),  # mv staging -> releases/<digest>
    Step("units", "abort"),  # install changed unit files + daemon-reload
    Step("caddy", "abort"),  # install Caddyfile + caddy validate/reload in staging
    Step("swap", "abort"),  # atomic current.next + rename - THE commit point
    Step("restart", "rollback"),  # restart per diff + converge instances
    Step("ready", "rollback"),  # readiness polling
    Step("confirm", "rollback"),  # registry write + fallback prune
)


def activation_plan(
    skip: Iterable[str] = (),
) -> list[Step]:
    """Ordered steps to run, minus the named-to-skips (e.g. no-op path)."""
    skipped = set(skip)
    return [step for step in ACTIVATION_STEPS if step.name not in skipped]


def first_rollback_step() -> int:
    """Index in ``ACTIVATION_STEPS`` of the first post-commit step."""
    for i, step in enumerate(ACTIVATION_STEPS):
        if step.on_failure == "rollback":
            return i
    raise AssertionError("no rollback step defined")


#: Rollback steps (kaze 02-store §10) - single level, failures stop loudly.
ROLLBACK_STEPS: tuple[Step, ...] = (
    Step("resolve", "abort"),  # verify target tree exists (GC'd = hard error)
    Step("units", "abort"),  # reinstall target's units if installations differ
    Step("caddy", "abort"),
    Step("swap", "abort"),
    Step("restart", "abort"),  # stop-and-report; NO auto-recursion on rollback
    Step("ready", "abort"),
    Step("confirm", "abort"),
)


def rollback_plan() -> list[Step]:
    return list(ROLLBACK_STEPS)


# --------------------------------------------------------------------------
# Restart set (fujin #79)
# --------------------------------------------------------------------------

_UNIT_SUFFIXES = (".service", ".socket", ".timer")
# dropin directories are ``<unitfile>.d/`` (e.g. ``app-web.service.d/``);
# ``common.d/`` applies to all services and is NOT a unit dropin.
_DROPIN_DIR_RE = re.compile(r"^(?P<unit>.+?\.(?:service|socket|timer))\.d/")


def unit_entry_name(path: str) -> str | None:
    """Map a manifest entry path to a systemctl unit name, or None.

    Paths live under ``systemd/``. Per-unit dropins are
    ``<unitfile>.d/<name>.conf`` and map to their parent unit;
    ``systemd/common.d/*`` (common dropins, apply to every unit) maps to
    ``None``. Returns the full filename (e.g. ``myapp-web.service``)
    without the directory prefix, template markers kept.
    """
    if not path.startswith("systemd/"):
        return None
    name = path[len("systemd/") :]
    if match := _DROPIN_DIR_RE.match(name):
        return match["unit"]
    if "/" not in name and name.endswith(_UNIT_SUFFIXES):
        return name
    return None


def _entry_identity(entry: Any) -> tuple:
    sha = getattr(entry, "sha256", None)
    link = getattr(entry, "link", None)
    mode = getattr(entry, "mode", None)
    kind = getattr(entry, "kind", None)
    return (kind, sha, link, mode)


def _changed_entries(
    prev: ManifestLike | None,
    new: ManifestLike | None,
) -> dict[str, str]:
    """Changed paths -> kind. Handles added/removed entries symmetrically."""
    prev_entries: Mapping[str, Any] = prev.entries if prev else {}
    new_entries: Mapping[str, Any] = new.entries if new else {}
    changed: dict[str, str] = {}
    for path, entry in prev_entries.items():
        other = new_entries.get(path)
        if other is None or _entry_identity(other) != _entry_identity(entry):
            changed[path] = str(entry.kind)
    for path, entry in new_entries.items():
        if path not in prev_entries:
            changed[path] = str(entry.kind)
    return changed


def restart_set(
    prev_manifest: ManifestLike | None,
    new_manifest: ManifestLike,
    env_fed_units: Iterable[str],
    oneshot_units: Iterable[str] = frozenset(),
) -> frozenset[str]:
    """Units to restart/reload for a deploy, per the #79 table.

    * ``unit`` entry changed -> exactly that unit (its instances) - full
      unit file name as it appears in the manifest (``systemd/`` prefix
      stripped, template markers kept). A changed ``common.d`` dropin
      applies to every unit, so it contributes the ``env_fed_units``. A
      *removed* unit contributes nothing (removal is the convergence/stop
      layer's job - restarting a nonexistent unit is an error).
    * ``env`` changed -> the ``env_fed_units`` (units fed by ``.env``).
    * ``code`` entries changed or the dist hash changed -> all app units
      that were fed env (== the app units; sockets/timers only restart when
      their own file changed). Implementations pass application units in
      ``env_fed_units``.
    * ``caddy`` -> reload only, contributes nothing here.
    * ``asset`` -> nothing.

    ``oneshot_units`` are excluded (Type=oneshot never restarts on deploy).
    """
    changed = _changed_entries(prev_manifest, new_manifest)
    env_fed = frozenset(env_fed_units)
    oneshots = frozenset(oneshot_units)
    restart: set[str] = set()
    env_changed = False
    prev_dist = normalize_digest(prev_manifest.dist_sha256) if prev_manifest else None
    code_changed = bool(prev_dist and new_manifest.dist_sha256) and (
        prev_dist != normalize_digest(new_manifest.dist_sha256)
    )
    for path, kind in changed.items():
        if kind == "unit":
            if path not in new_manifest.entries:
                continue  # removed unit: convergence stops it, never restart
            if name := unit_entry_name(path):
                restart.add(name)
            else:
                # common.d dropin (or non-unit path): applies to all units
                restart.update(env_fed)
        elif kind == "env":
            env_changed = True
        elif kind == "code":
            code_changed = True
        # asset/caddy: no restart contribution.
    if env_changed:
        restart.update(env_fed)
    if code_changed:
        restart.update(env_fed)
    return frozenset(restart - oneshots)


# --------------------------------------------------------------------------
# Convergence (template instances always converge, even in a no-op deploy)
# --------------------------------------------------------------------------


def desired_instances(service_name: str, replica_count: int) -> frozenset[str]:
    """Desired instance set for a replicated service: ``svc@1..N``.

    Never ``svc@.service`` and never the bare template - only instances are
    instantiable. ``replica_count <= 1`` yields a single ``svc@1`` rep-
    resenting that the service is instance-managed; a singular service
    (non-template unit) is passed through unchanged.
    """
    if replica_count < 1:
        raise ValueError(f"replica_count must be >= 1, got {replica_count}")
    template = f"{service_name}@"
    return frozenset(f"{template}{i}" for i in range(1, replica_count + 1))


_INSTANCE_INDEX_RE = re.compile(r"@(?P<index>\d+)$")


def instance_sort_key(name: str) -> int:
    """Numeric index of a template instance (``svc@3`` / ``svc@3.service``).

    A total ordering helper, not a validator: any name without a trailing
    numeric index - non-numeric instances (``svc@canary``), bare templates
    (``svc@``) and singular units - maps to ``0``. Used to order instance
    operations (stops go highest-first), so callers tie-break by name.
    """
    normalized = name.removesuffix(".service")
    match = _INSTANCE_INDEX_RE.search(normalized)
    if match is None:
        return 0
    return int(match.group("index"))


def _norm_instance(name: str) -> str:
    """Normalize an instance id: drop a trailing ``.service``, reject the
    bare template (``svc@`` / ``svc@.service``)."""
    name = name.removesuffix(".service")
    if name.endswith("@"):
        raise ValueError(f"bare template unit {name!r} must never be enabled")
    return name


def convergence(
    desired: Iterable[str],
    enabled: Iterable[str],
    active: Iterable[str],
) -> tuple[frozenset[str], frozenset[str]]:
    """Diff desired instances against the enabled and active views.

    Returns ``(start, stop)``. An instance needs ``enable --now`` whenever it
    is missing from EITHER view, so the start set is
    ``desired - (enabled & active)``: that covers absent, stopped-but-enabled
    and running-but-disabled instances. Everything visible in either view but
    not desired is stopped and disabled. Instance names are normalized by
    dropping a trailing ``.service`` so callers can pass either unit-file
    names or instance ids.
    """
    desired_set = frozenset(_norm_instance(n) for n in desired)
    enabled_set = frozenset(_norm_instance(n) for n in enabled)
    active_set = frozenset(_norm_instance(n) for n in active)
    present = enabled_set | active_set
    start = desired_set - (enabled_set & active_set)
    stop = present - desired_set
    return start, stop


# --------------------------------------------------------------------------
# GC plan (kaze 02-store §8)
# --------------------------------------------------------------------------


def gc_plan(
    current: str | None,
    previous: Iterable[str],
    releases: Iterable[str],
    versions_to_keep: int | None = None,
) -> tuple[str, ...]:
    """Digests to remove; keep current + previous[0] + newest by deploys order.

    ``versions_to_keep`` mirrors ``config.versions_to_keep``: ``None`` keeps
    everything (kill nothing); ``0``/``1`` keep only current + previous[0].
    ``releases`` is the newest-first ordering of every release digest known
    to the store (deploys order, falling back to directory mtime,
    determined by the caller). Pure. Deterministic output: sorted kill list.
    """
    if versions_to_keep is None:
        return ()
    releases = [normalize_digest(r) for r in releases]
    keep: set[str] = set()
    if current:
        keep.add(normalize_digest(current))
    previous = [normalize_digest(p) for p in previous]
    if previous:
        keep.add(previous[0])
    for release in releases:
        if len(keep) >= versions_to_keep:
            break
        keep.add(release)
    return tuple(sorted(r for r in releases if r not in keep))


def is_noop(
    client_digest: str,
    state: State,
) -> bool:
    """No-op deploy check (#78): client digest equals the live release's.

    Compares against ``state.client_digest`` (the client-side pre-merge
    digest recorded at last deploy); older state files without the field
    fall back to ``current``. A missing current (first deploy) is never a
    no-op; ``--force`` bypasses this check at the call site.
    """
    recorded = state.client_digest or state.current
    if not recorded:
        return False
    return normalize_digest(client_digest) == normalize_digest(recorded)
