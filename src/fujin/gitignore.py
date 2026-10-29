"""Helpers for keeping fujin's local build cache out of version control."""

from __future__ import annotations

from pathlib import Path

CACHE_PATTERN = "**/.fujin/cache/"

_CACHE_BLOCK = f"# fujin local build cache\n{CACHE_PATTERN}\n"

# Patterns that already cover the local build cache. Trailing slashes are
# stripped before comparing, so ``.fujin/cache`` and ``.fujin/cache/`` match.
_CACHE_EQUIVALENTS = {
    ".fujin/cache",
    "**/.fujin/cache",
    "/.fujin/cache",
}


def _ignores_cache(content: str) -> bool:
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.rstrip("/") in _CACHE_EQUIVALENTS:
            return True
    return False


def is_fujin_cache_ignored(project_root: Path = Path(".")) -> bool:
    """Return whether ``<project_root>/.gitignore`` already ignores the cache."""
    gitignore = project_root / ".gitignore"
    if not gitignore.exists():
        return False
    return _ignores_cache(gitignore.read_text(encoding="utf-8"))


def ensure_fujin_cache_ignored(project_root: Path = Path(".")) -> bool:
    """Add the local build cache to ``<project_root>/.gitignore``.

    Existing content is preserved byte-for-byte. Returns True only when the
    file was modified.
    """
    gitignore = project_root / ".gitignore"
    raw = gitignore.read_bytes() if gitignore.exists() else b""
    existing = raw.decode("utf-8")

    if _ignores_cache(existing):
        return False

    if not existing:
        updated = _CACHE_BLOCK
    elif existing.endswith("\n"):
        updated = existing + _CACHE_BLOCK
    else:
        updated = existing + "\n" + _CACHE_BLOCK

    gitignore.write_bytes(updated.encode("utf-8"))
    return True
