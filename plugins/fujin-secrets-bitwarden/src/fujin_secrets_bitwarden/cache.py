"""Per-user cache for the Bitwarden CLI session token.

Opt-in through ``[secrets.options] cache_session = true``. After an interactive
``bw unlock`` the adapter writes the session token to a temporary file, so a
later deploy - in the same tab or any other - reuses it instead of asking for
the master password again.

The cache is deliberately **not** tied to a terminal tab: a tab has no reliable
end-of-life signal, so a tab-keyed file would outlive the shell that created it
and still be readable by every other tab. Instead there is one file per user,
kept in ``$XDG_RUNTIME_DIR`` (tmpfs, removed at logout) when available. It
expires when Bitwarden invalidates the session: the adapter validates the
cached token with ``bw sync`` on every run, forgets it when the vault rejects
it, and unlocks again.

This is convenience, not a security boundary: the file is readable by any
process running as the same user. It is only written after an interactive
unlock and is mode ``0600`` inside a ``0700`` directory. Broken or unreadable
state is treated as absent, so a bad file can only force a re-unlock, never a
crash.
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

from fujin.config import SecretConfig
from fujin.errors import SecretResolutionError

CACHE_DIR_ENV = "FUJIN_BW_SESSION_DIR"

_TRUE = frozenset({"true", "yes", "1", "on"})
_FALSE = frozenset({"false", "no", "0", "off"})


def enabled(secret_config: SecretConfig) -> bool:
    """Whether caching is opted in for this adapter configuration."""
    raw = secret_config.options.get("cache_session")
    if raw is None:
        return False
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        normalized = raw.strip().lower()
        if normalized in _TRUE:
            return True
        if normalized in _FALSE:
            return False
    raise SecretResolutionError(
        "options.cache_session must be a boolean (true or false)",
        adapter="bitwarden",
    )


def default_cache_dir() -> Path:
    """Cache directory from the environment, without creating it."""
    override = os.environ.get(CACHE_DIR_ENV)
    if override:
        return Path(override).expanduser()
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / "fujin" / "bitwarden-session"
    getuid = getattr(os, "getuid", None)
    suffix = f"-{getuid()}" if getuid is not None else ""
    return Path(tempfile.gettempdir()) / f"fujin-bitwarden-session{suffix}"


def path() -> Path:
    """The cache file used by this user."""
    return default_cache_dir() / "session"


def load() -> str | None:
    """Return the cached session, or ``None`` when there is none."""
    target = path()
    try:
        info = target.lstat()
    except OSError:
        return None
    getuid = getattr(os, "getuid", None)
    if not stat.S_ISREG(info.st_mode):
        return None
    if getuid is not None and info.st_uid != getuid():
        return None
    if info.st_mode & 0o077:
        return None
    try:
        token = target.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None
    return token or None


def save(token: str) -> bool:
    """Persist a session with ``0600`` permissions.

    Best effort: an unwritable temp directory disables caching, it must never
    fail a deploy.
    """
    if not token:
        return False
    target = path()
    try:
        directory = target.parent
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        fd, temporary = tempfile.mkstemp(
            dir=directory, prefix=f".{target.name}.", suffix=".tmp"
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(token + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
    except OSError:
        return False
    return True


def clear() -> None:
    """Forget the cached session."""
    try:
        path().unlink()
    except OSError:
        pass
