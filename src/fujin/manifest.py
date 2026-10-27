"""Deterministic release-tree manifest and canonical hashing.

Pure logic only: no SSH, no subprocess, no filesystem access. Deploy code
feeds in file contents; this module classifies entries by source kind, hashes
them in sorted path order, and emits canonical manifest bytes.

Rules (see #77, #78 and the tier-1 contract):

- Entries are hashed sha256 in sorted path order; the release digest is
  ``sha256(canonical manifest bytes)``.
- Canonical JSON: sorted keys, compact separators, UTF-8, no timestamps.
- ``created_at`` is stored metadata and never enters the digest.
- Symlink entries record a ``link`` target instead of a ``sha256``.
- Server-built entries (``venv/**`` / ``bin/**``) are hashed server-side
  post-install and merged into ``manifest.json`` before commit; overall
  digest determinism is therefore relaxed for venv entries (recorded
  trade-off from #77).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from io import StringIO
from typing import Any

import msgspec
from dotenv import dotenv_values

from fujin.config import Config, HostConfig

# =============================================================================================
# Constants
# =============================================================================================

MANIFEST_SCHEMA = 1
MANIFEST_FILENAME = "manifest.json"

ENV_PATH = ".env"
CADDYFILE_PATH = "Caddyfile"
SYSTEMD_DIR = "systemd"
ASSETS_DIR = "assets"

DEFAULT_MODE_ASSET = 0o644
DEFAULT_MODE_CODE = 0o755
DEFAULT_MODE_ENV = 0o640
DEFAULT_MODE_SYMLINK = 0o777


# =============================================================================================
# Canonical hashing primitives
# =============================================================================================


def canonical_json(value: Any) -> bytes:
    """Serialize ``value`` to canonical JSON bytes (sorted keys, compact, UTF-8)."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    """Hex sha256 of ``data``."""
    return hashlib.sha256(data).hexdigest()


def hash_bytes(content: bytes | str) -> str:
    """Hex sha256 of file content (str is encoded as UTF-8)."""
    if isinstance(content, str):
        content = content.encode("utf-8")
    return sha256_hex(content)


# =============================================================================================
# Entries and kinds
# =============================================================================================


class EntryKind(StrEnum):
    CODE = "code"
    ASSET = "asset"
    UNIT = "unit"
    CADDY = "caddy"
    ENV = "env"


class Entry(msgspec.Struct, kw_only=True, frozen=True):
    """One file (or symlink) in the release tree.

    ``path`` is relative to the release root. ``mode`` is the permission
    bits (e.g. ``0o644``). Symlink entries carry ``link`` and no ``sha256``.
    """

    path: str
    sha256: str | None = None
    mode: int = DEFAULT_MODE_ASSET
    kind: EntryKind = EntryKind.CODE
    link: str | None = None


def default_mode(kind: EntryKind) -> int:
    """Default permission bits for a kind: code is executable, .env is group-readable only."""
    if kind is EntryKind.CODE:
        return DEFAULT_MODE_CODE
    if kind is EntryKind.ENV:
        return DEFAULT_MODE_ENV
    return DEFAULT_MODE_ASSET


def classify_path(path: str) -> EntryKind:
    """Classify a release-tree path by its source (kinds are assigned by source, never by config).

    Mirrors #77's table: the tree layout is a faithful mapping of sources.

    - ``.env`` -> env
    - ``Caddyfile`` -> caddy
    - ``systemd/...`` -> unit
    - ``assets/...`` -> asset
    - ``venv/**`` and ``bin/**`` -> code
    - anything else -> code (everything not spoken for above is app code)
    """
    path = path.strip("/")
    if path == ENV_PATH:
        return EntryKind.ENV
    if path == CADDYFILE_PATH:
        return EntryKind.CADDY
    if path == SYSTEMD_DIR or path.startswith(SYSTEMD_DIR + "/"):
        return EntryKind.UNIT
    if path == ASSETS_DIR or path.startswith(ASSETS_DIR + "/"):
        return EntryKind.ASSET
    return EntryKind.CODE


class InputFile(msgspec.Struct, frozen=True):
    """Client-known file content destined for the release tree.

    Exactly one of ``content`` / ``link`` must be set. ``mode=None`` uses
    the kind default; an explicit mode overrides the default (code stays
    executable by default so dist files never hit systemd 203/EXEC).
    """

    content: bytes | None = None
    mode: int | None = None
    link: str | None = None


def build_entry(path: str, spec: InputFile | str | bytes) -> Entry:
    """Build an Entry: assign kind from the tree path, hash content, apply mode defaults."""
    if not isinstance(spec, InputFile):
        spec = InputFile(
            content=spec.encode("utf-8") if isinstance(spec, str) else spec
        )
    if (spec.content is None) == (spec.link is None):
        raise ValueError(f"Entry {path!r} must have exactly one of content or link")
    kind = classify_path(path)
    mode = spec.mode if spec.mode is not None else default_mode(kind)
    if spec.link is not None:
        return Entry(path=path, mode=mode, kind=kind, link=spec.link, sha256=None)
    content = spec.content
    if content is None:  # pragma: no cover - guarded above
        raise ValueError(f"Entry {path!r} must have exactly one of content or link")
    return Entry(path=path, sha256=hash_bytes(content), mode=mode, kind=kind, link=None)


def build_manifest(
    *,
    app_name: str,
    version: str,
    config_fingerprint: str,
    dist_sha256: str,
    files: dict[str, InputFile | str | bytes],
    created_at: datetime | None = None,
) -> Manifest:
    """Assemble a Manifest from client-known files (kinds derived from tree paths)."""
    return Manifest(
        app_name=app_name,
        version=version,
        config_fingerprint=config_fingerprint,
        dist_sha256=dist_sha256,
        entries={path: build_entry(path, spec) for path, spec in files.items()},
        created_at=created_at,
    )


# =============================================================================================
# Manifest
# =============================================================================================


class Manifest(msgspec.Struct, kw_only=True, frozen=True):
    app_name: str
    version: str
    config_fingerprint: str
    dist_sha256: str
    entries: dict[str, Entry]
    created_at: datetime | None = None


def _entry_to_dict(entry: Entry) -> dict[str, Any]:
    data: dict[str, Any] = {
        "path": entry.path,
        "sha256": entry.sha256,
        "mode": f"{entry.mode:04o}",
        "kind": entry.kind.value,
    }
    if entry.link is not None:
        data["link"] = entry.link
    return data


def manifest_to_dict(
    manifest: Manifest, *, include_created_at: bool = True
) -> dict[str, Any]:
    """Serialize to a plain dict. ``created_at`` is included only when asked for."""
    data: dict[str, Any] = {
        "schema": MANIFEST_SCHEMA,
        "app_name": manifest.app_name,
        "version": manifest.version,
        "config_fingerprint": manifest.config_fingerprint,
        "dist_sha256": manifest.dist_sha256,
        "entries": {
            path: _entry_to_dict(entry)
            for path, entry in sorted(manifest.entries.items())
        },
    }
    if include_created_at and manifest.created_at is not None:
        data["created_at"] = manifest.created_at.isoformat()
    return data


def canonical_manifest_bytes(manifest: Manifest) -> bytes:
    """Canonical manifest bytes: the digest input. ``created_at`` is never included."""
    return canonical_json(manifest_to_dict(manifest, include_created_at=False))


def release_digest(manifest: Manifest) -> str:
    """sha256 of the canonical manifest bytes (created_at excluded)."""
    return sha256_hex(canonical_manifest_bytes(manifest))


def _entry_from_dict(data: dict[str, Any], *, path: str) -> Entry:
    sha256 = data.get("sha256")
    mode = data.get("mode")
    if isinstance(mode, str):
        mode = int(mode, 8)
    elif mode is None:
        mode = DEFAULT_MODE_ASSET
    kind = EntryKind(data.get("kind", EntryKind.CODE.value))
    return Entry(
        path=path,
        sha256=sha256,
        mode=mode,
        kind=kind,
        link=data.get("link"),
    )


def manifest_from_dict(data: dict[str, Any]) -> Manifest:
    """Decode a manifest dict (as stored in ``manifest.json``)."""
    created_at = data.get("created_at")
    if isinstance(created_at, str):
        created_at = datetime.fromisoformat(created_at)
    return Manifest(
        app_name=data["app_name"],
        version=data["version"],
        config_fingerprint=data["config_fingerprint"],
        dist_sha256=data["dist_sha256"],
        entries={
            path: _entry_from_dict(entry_data, path=path)
            for path, entry_data in data["entries"].items()
        },
        created_at=created_at,
    )


def decode_manifest(data: bytes | str) -> Manifest:
    """Decode manifest JSON bytes as stored on the server."""
    if isinstance(data, bytes):
        data = data.decode("utf-8")
    return manifest_from_dict(json.loads(data))


# =============================================================================================
# Diff
# =============================================================================================


class ChangeType(StrEnum):
    ADDED = "added"
    REMOVED = "removed"
    MODIFIED = "modified"


class EntryChange(msgspec.Struct, frozen=True):
    path: str
    change: ChangeType
    old: Entry | None = None
    new: Entry | None = None

    @property
    def kind(self) -> EntryKind:
        """Kind of the change: the new entry's kind for added/modified, old kind for removed."""
        entry = self.new if self.new is not None else self.old
        assert entry is not None
        return entry.kind


class Diff(msgspec.Struct, frozen=True):
    changes: list[EntryChange]

    @classmethod
    def from_changes(cls, changes: list[EntryChange]) -> Diff:
        return cls(changes=sorted(changes, key=lambda c: c.path))

    @property
    def is_empty(self) -> bool:
        return not self.changes

    @property
    def kinds_changed(self) -> set[EntryKind]:
        return {change.kind for change in self.changes}

    def grouped(self) -> dict[EntryKind, list[EntryChange]]:
        """Changes grouped by kind; every kind is present (possibly empty)."""
        grouped: dict[EntryKind, list[EntryChange]] = {kind: [] for kind in EntryKind}
        for change in self.changes:
            grouped[change.kind].append(change)
        return grouped

    def by_kind(self, kind: EntryKind) -> list[EntryChange]:
        return [change for change in self.changes if change.kind is kind]


def diff_manifests(prev: Manifest, new: Manifest) -> Diff:
    """Diff two manifests, grouped by kind (input to restart-set / no-op detection)."""
    changes: list[EntryChange] = []
    for path in sorted(set(prev.entries) | set(new.entries)):
        old = prev.entries.get(path)
        entry = new.entries.get(path)
        if old is None and entry is None:  # pragma: no cover - unreachable
            continue
        if old is None:
            changes.append(EntryChange(path=path, change=ChangeType.ADDED, new=entry))
        elif entry is None:
            changes.append(EntryChange(path=path, change=ChangeType.REMOVED, old=old))
        elif old != entry:
            changes.append(
                EntryChange(path=path, change=ChangeType.MODIFIED, old=old, new=entry)
            )
    return Diff.from_changes(changes)


# =============================================================================================
# Config fingerprint
# =============================================================================================


def _env_keys(env_content: str) -> list[str]:
    """Variable names declared in an env file; VALUES are secret-carriers and are dropped."""
    if not env_content:
        return []
    return sorted(dotenv_values(stream=StringIO(env_content)) or {})


def _fingerprint_host(config: Config, host: HostConfig) -> dict[str, Any]:
    return {
        "address": host.address,
        "user": host.user,
        "port": host.port,
        "native_ssh_auth": host.native_ssh_auth,
        "key_passphrase_env": host.key_passphrase_env,
        # Key NAMES of the host's effective env (project template + host
        # overlay); values stay out (secret carriers).
        "env_keys": _env_keys(config.effective_env(host)),
    }


def _fingerprint_data(config: Config) -> dict[str, Any]:
    """Resolved-config identity dict. Explicit include-list: excludes version,
    replicas, aliases, hooks, local paths, and all secret configuration
    (values and adapter settings alike)."""
    return {
        "app_name": config.app_name,
        "app_user": config.app_user,
        "installation_mode": str(config.installation_mode),
        "python_version": config.python_version,
        "build_command": config.build_command,
        "build_timeout": config.build_timeout,
        "distfile": config.distfile,
        "requirements": config.requirements,
        "apps_dir": config.apps_dir,
        "caddy_config_dir": config.caddy_config_dir,
        "hosts": [_fingerprint_host(config, host) for host in config.hosts],
    }


def config_fingerprint(config: Config) -> str:
    """Stable hash of the resolved configuration.

    Canonical resolved-config JSON (sorted keys), excluding:

    - secret VALUES and adapter settings: the host's effective env template
      (``Config.effective_env``, i.e. ``env``/``envfile`` read during
      ``__post_init__``, project template merged with the host overlay) is
      reduced to its variable NAMES only, and the global/per-host
      ``[secrets]`` adapter configuration is omitted entirely: adapter
      identity is not a build input (the venv installs from requirements;
      resolved VALUES land in the rendered ``.env``, whose change already
      flips the release digest).
    - ``replicas`` (per #79 convergence handles scaling separately);
    - ``version``, ``aliases``, ``hooks_config``, ``local_config_dir``
      (release-identity / local-only knobs that must not bust build caches).
    """
    return sha256_hex(canonical_json(_fingerprint_data(config)))


# =============================================================================================
# Bundle key (build cache, #81)
# =============================================================================================


def bundle_key(
    config: Config,
    *,
    unit_sources: dict[str, str | bytes],
    assets: dict[str, str | bytes] | None = None,
    caddyfile: str | bytes | None = None,
    build_env: dict[str, str] | None = None,
    requirements: str | bytes | None = None,
    pyproject: str | bytes | None = None,
) -> str:
    """Content-addressed key over build inputs.

    sha256 over canonical JSON of: the config fingerprint, ``build_command``,
    the build environment, the requirements file content, and the hashes of
    every bundled unit/dropin/Caddyfile/asset source. The version IS a cache
    input (the built dist embeds it: wheel names/metadata, binary build
    metadata) even though ``config_fingerprint`` excludes it. Rendered ``.env``
    content is deliberately NOT an input: env is applied after the cache
    lookup and secret values never reach build artifacts (#81 hard boundary).
    """
    sources: dict[str, str] = {
        f"unit/{path}": hash_bytes(content) for path, content in unit_sources.items()
    }
    if assets:
        sources.update(
            {f"asset/{path}": hash_bytes(content) for path, content in assets.items()}
        )
    if caddyfile is not None:
        sources["caddy"] = hash_bytes(caddyfile)
    if pyproject is not None:
        # pyproject.toml is the source that drives generated requirements
        # (uv pip compile ...) and general dist contents; hashing it covers
        # the window where build_command regenerates requirements.txt
        # between bundle keys being computed (generated-requirements mode).
        sources["pyproject"] = hash_bytes(pyproject)
    payload = {
        "bundle": MANIFEST_SCHEMA,
        "config_fingerprint": config_fingerprint(config),
        "build_command": config.build_command,
        "build_env": build_env or {},
        "installation_mode": str(config.installation_mode),
        "python_version": config.python_version,
        "version": config.version,
        "requirements": None if requirements is None else hash_bytes(requirements),
        "sources": sources,
    }
    return sha256_hex(canonical_json(payload))
