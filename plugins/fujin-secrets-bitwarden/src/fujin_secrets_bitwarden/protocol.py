"""Bitwarden/Vaultwarden secret adapter using the HTTP protocol directly.

The ``bitwarden`` adapter shells out to the Bitwarden CLI. This adapter talks
to the server's public endpoints (prelogin, identity token, sync) and performs
the vault crypto itself, so deployments need neither the ``bw`` binary nor a
pre-unlocked session.

Scope is deliberately narrow: **read-only**, personal vault, login passwords.
No item creation/update, no organizations/collections, no attachments. Two-
factor accounts are supported through a trusted device seeded once by
``fujin-secrets-bitwarden-login``: the adapter itself never prompts and only
reuses the remember token persisted for the account (see ``store``).

Configuration (all under ``[secrets.options]``):

- ``email`` (required): account email; normalized to lower case for the KDF.
- ``password_env`` (required): environment variable holding the master
  password. ``BW_SESSION`` is not accepted: a token alone cannot decrypt the
  vault, the master password is always needed locally.
- ``server_url`` (optional): defaults to ``https://vault.bitwarden.com``.
- ``folders`` (optional): folder lookup order, same semantics as the CLI
  adapter. Without it, items are resolved by name across the whole vault and
  duplicate names fail closed.

Errors never include vault content, decrypted values or response bodies.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeGuard

from argon2.exceptions import HashingError
from argon2.low_level import Type as Argon2Type
from argon2.low_level import hash_secret_raw
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7
from fujin.config import SecretConfig
from fujin.errors import SecretResolutionError
from fujin.secrets import render_env, split_env_template

from . import _folders_option, _split_ref, store

_ADAPTER = "bitwarden-protocol"
_SEED_COMMAND = "fujin-secrets-bitwarden-login"
_DEFAULT_SERVER_URL = "https://vault.bitwarden.com"
_TIMEOUT = 15
_KEY_BYTES = 32
_USER_KEY_BYTES = 64
_BLOCK_BITS = 128

# Bitwarden KDF identifiers returned by ``identity/accounts/prelogin``.
_KDF_PBKDF2_SHA256 = 0
_KDF_ARGON2ID = 1


# ---------------------------------------------------------------------------
# Crypto
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Kdf:
    """KDF parameters as published by the server's prelogin endpoint."""

    kind: int
    iterations: int
    memory: int = 0
    parallelism: int = 0


def pbkdf2_sha256(
    secret: bytes, salt: bytes, iterations: int, length: int = _KEY_BYTES
) -> bytes:
    """PBKDF2-HMAC-SHA256 derived key."""
    return hashlib.pbkdf2_hmac("sha256", secret, salt, iterations, dklen=length)


def argon2id(
    secret: bytes,
    salt: bytes,
    iterations: int,
    memory: int,
    parallelism: int,
    length: int = _KEY_BYTES,
) -> bytes:
    """Argon2id derived key, matching the Bitwarden client parameters.

    ``memory`` is in KiB, ``iterations`` is Argon2's time cost and the version
    is fixed to 0x13 (19), as used by all Bitwarden clients.
    """
    return hash_secret_raw(
        secret=secret,
        salt=salt,
        time_cost=iterations,
        memory_cost=memory,
        parallelism=parallelism,
        hash_len=length,
        type=Argon2Type.ID,
        version=19,
    )


def derive_master_key(password: str, email: str, kdf: Kdf) -> bytes:
    """Derive the 32-byte master key from the master password.

    ``email`` is the normalized (stripped, lower-cased) account email.
    Unknown KDF identifiers fail closed.
    """
    if kdf.kind == _KDF_PBKDF2_SHA256:
        return pbkdf2_sha256(
            password.encode("utf-8"), email.encode("utf-8"), kdf.iterations
        )
    if kdf.kind == _KDF_ARGON2ID:
        try:
            return argon2id(
                password.encode("utf-8"),
                email.encode("utf-8"),
                kdf.iterations,
                kdf.memory,
                kdf.parallelism,
            )
        except (HashingError, ValueError) as exc:
            raise _failure("Failed to derive the Bitwarden master key") from exc
    raise _failure("Bitwarden server uses an unsupported KDF")


def master_password_hash(master_key: bytes, password: str) -> bytes:
    """The login ``password``: one PBKDF2 round over the master key."""
    return pbkdf2_sha256(master_key, password.encode("utf-8"), 1)


def hkdf_expand(prk: bytes, info: bytes, length: int = _KEY_BYTES) -> bytes:
    """HKDF-Expand (RFC 5869) with SHA-256, without the extract step."""
    if length < 0 or length > 255 * 32:
        raise ValueError("invalid HKDF output length")
    output = b""
    block = b""
    counter = 1
    while len(output) < length:
        block = hmac.new(prk, block + info + bytes((counter,)), hashlib.sha256).digest()
        output += block
        counter += 1
    return output[:length]


def stretch_keys(master_key: bytes) -> tuple[bytes, bytes]:
    """Stretch the master key into ``(encKey, macKey)``."""
    return (
        hkdf_expand(master_key, b"enc", _KEY_BYTES),
        hkdf_expand(master_key, b"mac", _KEY_BYTES),
    )


def _decode_b64(segment: str, what: str) -> bytes:
    try:
        return base64.b64decode(segment, validate=True)
    except (ValueError, TypeError) as exc:
        raise _failure(f"Malformed Bitwarden {what}") from exc


def decrypt_enc_string(value: str, enc_key: bytes, mac_key: bytes) -> bytes:
    """Decrypt a Bitwarden EncString with the given symmetric keys.

    Supported types are ``2`` (AesCbc256_HmacSha256_B64, MAC required) and
    ``0`` (AesCbc256_B64, no MAC). Anything else fails closed.
    """
    if not isinstance(value, str):
        raise _failure("Malformed Bitwarden encrypted value")
    enc_type, separator, payload = value.partition(".")
    if not separator or not payload:
        raise _failure("Malformed Bitwarden encrypted value")

    if enc_type == "2":
        parts = payload.split("|")
        if len(parts) != 3:
            raise _failure("Malformed Bitwarden encrypted value")
        iv_b64, ciphertext_b64, mac_b64 = parts
    elif enc_type == "0":
        parts = payload.split("|")
        if len(parts) != 2:
            raise _failure("Malformed Bitwarden encrypted value")
        iv_b64, ciphertext_b64 = parts
        mac_b64 = None
    else:
        raise _failure("Unsupported Bitwarden encryption type")

    iv = _decode_b64(iv_b64, "encrypted value")
    ciphertext = _decode_b64(ciphertext_b64, "encrypted value")
    if len(iv) != 16 or not ciphertext or len(ciphertext) % 16:
        raise _failure("Malformed Bitwarden encrypted value")

    if mac_b64 is not None:
        mac = _decode_b64(mac_b64, "encrypted value")
        expected = hmac.new(mac_key, iv + ciphertext, hashlib.sha256).digest()
        if not hmac.compare_digest(mac, expected):
            raise _failure("Bitwarden encrypted value failed MAC verification")

    try:
        decryptor = Cipher(algorithms.AES(enc_key), modes.CBC(iv)).decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        unpadder = PKCS7(_BLOCK_BITS).unpadder()
        return unpadder.update(padded) + unpadder.finalize()
    except ValueError as exc:
        raise _failure("Failed to decrypt a Bitwarden value") from exc


def decrypt_string(value: str, enc_key: bytes, mac_key: bytes) -> str:
    """Decrypt an EncString that holds UTF-8 text."""
    try:
        return decrypt_enc_string(value, enc_key, mac_key).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _failure("Failed to decrypt a Bitwarden value") from exc


def _unwrap_user_key(key: str, enc_key: bytes, mac_key: bytes) -> tuple[bytes, bytes]:
    """Decrypt the account key and split it into user enc/mac keys."""
    raw = decrypt_enc_string(key, enc_key, mac_key)
    if len(raw) != _USER_KEY_BYTES:
        raise _failure("Bitwarden account key has an unexpected length")
    return raw[:_KEY_BYTES], raw[_KEY_BYTES:]


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


def _failure(message: str, key: str | None = None) -> SecretResolutionError:
    """Value-free failure attributed to this adapter."""
    return SecretResolutionError(message, adapter=_ADAPTER, key=key)


class TwoFactorRequired(SecretResolutionError):
    """The password grant needs a second factor that was not accepted."""


@dataclass(frozen=True)
class TokenResult:
    """Decoded identity token response."""

    access_token: str
    key: str
    remember_token: str | None = None


def _seed_hint(state_path: Path) -> str:
    """Value-free instruction to seed a trusted device for this account."""
    return (
        "Bitwarden login requires two-factor authentication. Run "
        f"`{_SEED_COMMAND}` to seed (or re-seed) a trusted device for this "
        f"account (state file: {state_path}), then retry the deploy."
    )


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


class _Client:
    """Minimal HTTP client for the three endpoints the adapter needs."""

    def __init__(self, server_url: str):
        self._server_url = server_url

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str],
        action: str,
    ) -> tuple[int, bytes]:
        request = urllib.request.Request(
            self._server_url + path, data=body, method=method, headers=headers
        )
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            # The body is only inspected for a 2FA hint; it is never echoed.
            try:
                payload = exc.read()
            except OSError:
                payload = b""
            return exc.code, payload
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise _failure(
                f"Failed to reach the Bitwarden server for {action}"
            ) from exc

    def _decode_json(self, payload: bytes, action: str) -> dict:
        try:
            data = json.loads(payload)
        except ValueError as exc:
            raise _failure(
                f"Unexpected response from the Bitwarden server for {action}"
            ) from exc
        if not isinstance(data, dict):
            raise _failure(
                f"Unexpected response from the Bitwarden server for {action}"
            )
        return data

    def prelogin(self, email: str) -> Kdf:
        """Fetch the account's KDF parameters."""
        body = json.dumps({"email": email}).encode("utf-8")
        status, payload = self._request(
            "POST",
            "/identity/accounts/prelogin",
            body=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            action="the Bitwarden KDF settings",
        )
        if not 200 <= status < 300:
            raise _failure(
                f"Bitwarden request failed for the KDF settings (HTTP {status})"
            )
        data = self._decode_json(payload, "the KDF settings")

        kind = data.get("Kdf")
        iterations = data.get("KdfIterations")
        if not _is_int(kind) or kind not in (_KDF_PBKDF2_SHA256, _KDF_ARGON2ID):
            raise _failure("Bitwarden server uses an unsupported KDF")
        if not _is_int(iterations) or iterations < 1:
            raise _failure("Bitwarden server returned invalid KDF parameters")
        memory = data.get("KdfMemory")
        parallelism = data.get("KdfParallelism")
        if kind == _KDF_ARGON2ID:
            if (
                not _is_int(memory)
                or memory < 1
                or not _is_int(parallelism)
                or parallelism < 1
            ):
                raise _failure(
                    "Bitwarden server returned invalid Argon2 KDF parameters"
                )
        return Kdf(
            kind=kind,
            iterations=iterations,
            memory=memory if _is_int(memory) else 0,
            parallelism=parallelism if _is_int(parallelism) else 0,
        )

    def login(
        self,
        email: str,
        password_hash: bytes,
        *,
        device_identifier: str,
        two_factor_token: str | None = None,
        two_factor_provider: int | None = None,
        two_factor_code: str | None = None,
        remember: bool = False,
    ) -> TokenResult:
        """Exchange credentials for a token, using a second factor if needed.

        Three mutually exclusive shapes are supported:

        - plain password grant (no second factor),
        - a one-time code (``two_factor_code``) to enrol this device, with
          ``twoFactorRemember`` set when ``remember`` is requested,
        - an already trusted device (``two_factor_token``), sent with provider
          5 (remember) so no code is needed.

        The persisted ``device_identifier`` is always sent: remember tokens are
        bound to it.
        """
        fields: dict[str, str] = {
            "grant_type": "password",
            "username": email,
            "password": base64.b64encode(password_hash).decode("ascii"),
            "scope": "api offline_access",
            "client_id": "cli",
            "deviceType": "10",
            "deviceIdentifier": device_identifier,
            "deviceName": "fujin",
        }
        if two_factor_code:
            fields["twoFactorToken"] = two_factor_code
            fields["twoFactorProvider"] = str(
                0 if two_factor_provider is None else two_factor_provider
            )
            if remember:
                fields["twoFactorRemember"] = "1"
        elif two_factor_token:
            fields["twoFactorToken"] = two_factor_token
            fields["twoFactorProvider"] = "5"
            fields["twoFactorRemember"] = "0"

        status, payload = self._request(
            "POST",
            "/identity/connect/token",
            body=urllib.parse.urlencode(fields).encode("utf-8"),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            action="login",
        )
        if not 200 <= status < 300:
            if _has_two_factor_hint(payload):
                raise TwoFactorRequired(
                    "Bitwarden login requires two-factor authentication"
                )
            raise _failure(f"Bitwarden login failed (HTTP {status})")

        data = self._decode_json(payload, "login")
        token = data.get("access_token")
        key = data.get("Key")
        if not isinstance(token, str) or not token:
            raise _failure("Bitwarden login response did not include an access token")
        if not isinstance(key, str) or not key:
            raise _failure("Bitwarden login response did not include the account key")
        remember_token = data.get("TwoFactorToken")
        return TokenResult(
            access_token=token,
            key=key,
            remember_token=remember_token
            if isinstance(remember_token, str) and remember_token
            else None,
        )

    def sync(self, token: str) -> dict:
        """Fetch the vault (folders and ciphers) for the authenticated user."""
        status, payload = self._request(
            "GET",
            "/api/sync",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
            action="the vault sync",
        )
        if not 200 <= status < 300:
            raise _failure(
                f"Bitwarden request failed for the vault sync (HTTP {status})"
            )
        return self._decode_json(payload, "the vault sync")


def _has_two_factor_hint(payload: bytes) -> bool:
    """Detect a 2FA marker without echoing any part of the body."""
    lowered = payload.lower()
    return b"twofactor" in lowered or b"two-factor" in lowered or b"2fa" in lowered


# ---------------------------------------------------------------------------
# Vault model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Item:
    name: str
    folder_id: str | None
    password: str


@dataclass(frozen=True)
class _Folder:
    id: str
    name: str


class _Vault:
    """Decrypted, read-only view of the vault's login passwords."""

    def __init__(
        self,
        raw_folders: list[dict],
        raw_ciphers: list[dict],
        enc_key: bytes,
        mac_key: bytes,
    ):
        self._folders: list[_Folder] = []
        for entry in raw_folders:
            folder_id = entry.get("id")
            encrypted_name = entry.get("name")
            if not isinstance(folder_id, str) or not isinstance(encrypted_name, str):
                continue
            self._folders.append(
                _Folder(
                    id=folder_id,
                    name=decrypt_string(encrypted_name, enc_key, mac_key),
                )
            )

        self._folder_index: dict[str, str] | None = None
        self._items_by_folder: dict[str, dict[str, str]] = {}
        self._items: list[_Item] = []
        for cipher in raw_ciphers:
            # Organization items are encrypted with org/item keys this adapter
            # does not implement; skipping keeps personal-vault resolution
            # usable instead of failing on unrelated items.
            if cipher.get("organizationId") or cipher.get("key"):
                continue
            # Only login items with a password take part; notes, cards,
            # identities and passwordless logins are skipped.
            if cipher.get("type") != 1:
                continue
            login = cipher.get("login")
            if not isinstance(login, dict):
                continue
            encrypted_name = cipher.get("name")
            encrypted_password = login.get("password")
            if not isinstance(encrypted_name, str) or not isinstance(
                encrypted_password, str
            ):
                continue
            if not encrypted_password:
                continue
            folder_id = cipher.get("folderId")
            self._items.append(
                _Item(
                    name=decrypt_string(encrypted_name, enc_key, mac_key),
                    folder_id=folder_id if isinstance(folder_id, str) else None,
                    password=decrypt_string(encrypted_password, enc_key, mac_key),
                )
            )

    def folder_names(self) -> dict[str, str]:
        """Map folder name -> id, failing closed on duplicate names."""
        if self._folder_index is None:
            index: dict[str, str] = {}
            for folder in self._folders:
                if folder.name in index:
                    raise _failure(
                        f"Bitwarden folder name '{folder.name}' is duplicated; "
                        "folder-scoped resolution by name is ambiguous"
                    )
                index[folder.name] = folder.id
            self._folder_index = index
        return self._folder_index

    def items_in(self, folder: str) -> dict[str, str]:
        """Exact-name item map for a folder; first match wins."""
        cached = self._items_by_folder.get(folder)
        if cached is not None:
            return cached
        folder_id = self.folder_names().get(folder)
        if folder_id is None:
            raise _failure(f"Bitwarden folder '{folder}' was not found")
        items: dict[str, str] = {}
        for item in self._items:
            if item.folder_id == folder_id:
                items.setdefault(item.name, item.password)
        self._items_by_folder[folder] = items
        return items

    def resolve(
        self, refs: dict[str, str], folders: list[str] | None
    ) -> dict[str, str]:
        if folders is None:
            return self._resolve_global(refs)
        return self._resolve_folders(refs, folders)

    def _resolve_global(self, refs: dict[str, str]) -> dict[str, str]:
        """Resolve refs by exact item name across the whole vault."""
        by_name: dict[str, list[str]] = {}
        for item in self._items:
            by_name.setdefault(item.name, []).append(item.password)

        resolved: dict[str, str] = {}
        for key, ref in refs.items():
            folder, name = _split_ref(ref)
            if folder is not None:
                resolved[key] = self._from_folder(key, ref, folder, name)
                continue
            matches = by_name.get(name, [])
            if not matches:
                raise _failure(
                    f"Secret for {key} (ref '{ref}') was not found in the "
                    "Bitwarden vault",
                    key=key,
                )
            if len(matches) > 1:
                raise _failure(
                    f"Secret for {key} (ref '{ref}') matches multiple Bitwarden "
                    "items; use a folder-prefixed ref or a unique item name",
                    key=key,
                )
            resolved[key] = matches[0]
        return resolved

    def _resolve_folders(
        self, refs: dict[str, str], folders: list[str]
    ) -> dict[str, str]:
        """Resolve refs against configured folders, in configured order."""
        # Eager existence check: an unknown configured folder fails before any
        # ref is matched, mirroring the CLI adapter.
        available = self.folder_names()
        for configured in folders:
            if configured not in available:
                raise _failure(f"Bitwarden folder '{configured}' was not found")

        resolved: dict[str, str] = {}
        for key, ref in refs.items():
            folder, name = _split_ref(ref)
            if folder is not None:
                resolved[key] = self._from_folder(key, ref, folder, name)
                continue
            for candidate in folders:
                items = self.items_in(candidate)
                if name in items:
                    resolved[key] = items[name]
                    break
            else:
                raise _missing_secret(key, ref, folders)
        return resolved

    def _from_folder(self, key: str, ref: str, folder: str, name: str) -> str:
        if folder not in self.folder_names():
            raise _missing_secret(key, ref, [folder])
        items = self.items_in(folder)
        if name not in items:
            raise _missing_secret(key, ref, [folder])
        return items[name]


def _missing_secret(key: str, ref: str, folders: list[str]) -> SecretResolutionError:
    """Value-free failure naming the ref/key and the folders that were tried."""
    tried = ", ".join(f"'{folder}'" for folder in folders)
    return _failure(
        f"Secret for {key} (ref '{ref}') was not found in Bitwarden folder(s): {tried}",
        key=key,
    )


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _required_option(secret_config: SecretConfig, name: str) -> str:
    value = secret_config.options.get(name)
    if not isinstance(value, str) or not value.strip():
        raise _failure(f"options.{name} is required by the bitwarden-protocol adapter")
    return value


def _server_url(secret_config: SecretConfig) -> str:
    value = secret_config.options.get("server_url")
    if value is None:
        return _DEFAULT_SERVER_URL
    if not isinstance(value, str) or not value.strip():
        raise _failure("options.server_url must be a non-empty URL")
    return normalize_server_url(value)


def normalize_server_url(value: str) -> str:
    """Validate a server URL and strip its trailing slash."""
    if not value.strip():
        raise _failure("options.server_url must be a non-empty URL")
    url = value.strip().rstrip("/")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise _failure("options.server_url must be an http(s) URL")
    return url


# ---------------------------------------------------------------------------
# Adapter entry point
# ---------------------------------------------------------------------------


def bitwarden_protocol(env_content: str, secret_config: SecretConfig) -> str:
    """Resolve ``$REF`` values from a personal Bitwarden/Vaultwarden vault.

    Uses the server HTTP API directly (prelogin, token, sync) and decrypts the
    vault locally. Read-only; no token is stored or reused between runs.
    """
    values, refs = split_env_template(env_content)

    email = _required_option(secret_config, "email").strip().lower()
    password_env = _required_option(secret_config, "password_env")
    server_url = _server_url(secret_config)

    # No refs means no vault access at all: literals render untouched.
    if not refs:
        return render_env(values)
    folders = _folders_option(secret_config)

    password = os.environ.get(password_env)
    if not password:
        raise _failure(
            f"Environment variable '{password_env}' is not set; the "
            "bitwarden-protocol adapter needs the master password to decrypt "
            "the vault"
        )

    client = _Client(server_url)
    kdf = client.prelogin(email)
    master_key = derive_master_key(password, email, kdf)
    enc_key, mac_key = stretch_keys(master_key)

    state_path = store.path_for(server_url, email)
    device = store.load(server_url, email)
    if device is not None and device.is_expired():
        # Never send a stale token: the server would reject it anyway and the
        # user needs to re-seed either way.
        raise _failure(
            "The stored Bitwarden trusted device is no longer valid. Re-seed "
            f"it with `{_SEED_COMMAND}` (state file: {state_path}), then retry "
            "the deploy."
        )
    device_identifier = (
        device.device_identifier if device is not None else str(uuid.uuid4())
    )
    try:
        result = client.login(
            email,
            master_password_hash(master_key, password),
            device_identifier=device_identifier,
            two_factor_token=device.two_factor_token if device is not None else None,
        )
    except TwoFactorRequired as exc:
        raise _failure(_seed_hint(state_path)) from exc

    if result.remember_token:
        store.save(
            store.RememberedDevice(
                server_url=server_url,
                email=email,
                device_identifier=device_identifier,
                two_factor_token=result.remember_token,
                obtained_at=datetime.now(UTC),
            )
        )

    user_enc_key, user_mac_key = _unwrap_user_key(result.key, enc_key, mac_key)
    data = client.sync(result.access_token)

    vault = _Vault(
        _entries(data.get("folders"), "folders"),
        _entries(data.get("ciphers"), "ciphers"),
        user_enc_key,
        user_mac_key,
    )
    values.update(vault.resolve(refs, folders))
    return render_env(values)


def _entries(value: object, what: str) -> list[dict]:
    """Normalize a sync list field, failing closed on unexpected shapes."""
    if value is None:
        return []
    if not isinstance(value, list) or not all(
        isinstance(entry, dict) for entry in value
    ):
        raise _failure(f"Unexpected response from the Bitwarden server for {what}")
    return value
