"""Persistent trusted-device state for the ``bitwarden-protocol`` adapter.

Bitwarden lets a client remember a two-factor login: after one interactive
login with a second factor and ``twoFactorRemember=1``, the token response
carries a *remember token* that later password grants can present as
``twoFactorToken`` with ``twoFactorProvider=5``. That token is bound to the
``deviceIdentifier`` used when it was issued, so both are stored together.

One JSON file per account is kept under the state directory:

- ``$FUJIN_BW_STATE_DIR`` when set (used by tests and for relocatable setups),
- ``$XDG_STATE_HOME/fujin/bitwarden-protocol`` otherwise,
- ``~/.local/state/fujin/bitwarden-protocol`` as the last resort.

The file holds the server URL, the normalized account email, the device
identifier, the remember token and the time it was obtained. It never holds
the master password or an access token. Corrupt or incomplete state is treated
as absent so a bad file can only ever force a re-seed, never a crash.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

STATE_DIR_ENV = "FUJIN_BW_STATE_DIR"
MAX_AGE = timedelta(days=29)


def _normalize_email(email: str) -> str:
    return email.strip().lower()


def default_state_dir() -> Path:
    """State directory from the environment, without creating it."""
    override = os.environ.get(STATE_DIR_ENV)
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "state"
    return base / "fujin" / "bitwarden-protocol"


def account_digest(server_url: str, email: str) -> str:
    """Stable per-account file stem, independent of email casing."""
    raw = f"{server_url}\n{_normalize_email(email)}".encode()
    return hashlib.sha256(raw).hexdigest()[:12]


def path_for(server_url: str, email: str, state_dir: Path | None = None) -> Path:
    directory = state_dir if state_dir is not None else default_state_dir()
    return directory / f"bitwarden-protocol-{account_digest(server_url, email)}.json"


@dataclass(frozen=True)
class RememberedDevice:
    """A trusted device that can satisfy the password grant's 2FA challenge."""

    server_url: str
    email: str
    device_identifier: str
    two_factor_token: str
    obtained_at: datetime

    def is_expired(self, *, now: datetime | None = None) -> bool:
        """Whether the server would no longer accept this remember token."""
        current = now if now is not None else datetime.now(UTC)
        obtained = self.obtained_at
        if obtained.tzinfo is None:
            obtained = obtained.replace(tzinfo=UTC)
        return current - obtained > MAX_AGE

    def to_json(self) -> dict[str, str]:
        return {
            "server_url": self.server_url,
            "email": self.email,
            "device_identifier": self.device_identifier,
            "two_factor_token": self.two_factor_token,
            "obtained_at": _format_time(self.obtained_at),
        }


def _format_time(value: datetime) -> str:
    moment = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _required_string(data: dict, name: str) -> str | None:
    value = data.get(name)
    if isinstance(value, str) and value:
        return value
    return None


def device_from_json(data: object) -> RememberedDevice | None:
    """Validate a decoded state file; anything unexpected means *absent*."""
    if not isinstance(data, dict):
        return None
    server_url = _required_string(data, "server_url")
    email = _required_string(data, "email")
    device_identifier = _required_string(data, "device_identifier")
    two_factor_token = _required_string(data, "two_factor_token")
    obtained_at = _parse_time(data.get("obtained_at"))
    if None in (server_url, email, device_identifier, two_factor_token, obtained_at):
        return None
    return RememberedDevice(
        server_url=server_url,
        email=_normalize_email(email),
        device_identifier=device_identifier,
        two_factor_token=two_factor_token,
        obtained_at=obtained_at,
    )


def load(
    server_url: str, email: str, state_dir: Path | None = None
) -> RememberedDevice | None:
    """Load the remembered device for an account, or ``None``."""
    path = path_for(server_url, email, state_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return device_from_json(data)


def _write_atomically(path: Path, text: str) -> None:
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    fd, temporary = tempfile.mkstemp(
        dir=directory, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def save(device: RememberedDevice, state_dir: Path | None = None) -> Path:
    """Persist a remembered device with 0600 permissions; returns its path."""
    path = path_for(device.server_url, device.email, state_dir)
    _write_atomically(path, json.dumps(device.to_json(), sort_keys=True) + "\n")
    return path


def clear(server_url: str, email: str, state_dir: Path | None = None) -> bool:
    """Remove the stored device; returns whether a file was removed."""
    path = path_for(server_url, email, state_dir)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True
