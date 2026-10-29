"""Bitwarden secret adapter for Fujin."""

from __future__ import annotations

import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

from fujin.config import SecretConfig
from fujin.errors import SecretResolutionError
from fujin.secrets import render_env, split_env_template


def bitwarden(env_content: str, secret_config: SecretConfig) -> str:
    """Bitwarden secret adapter.

    Retrieves secrets from Bitwarden vault using the `bw` CLI.

    Configuration:
        - Requires BW_SESSION environment variable, OR
        - Set password_env to unlock vault using environment variable
        - Optional folders: lookup order for folder-scoped resolution

    Example fujin.toml:
        [secrets]
        adapter = "bitwarden"

        [secrets.options]
        password_env = "BW_PASSWORD"  # Optional
        folders = ["fujin/myapp/prod", "fujin/shared"]  # Optional

    Without ``folders`` every ``$REF`` is resolved by name across the whole
    vault (one ``bw get password`` per ref, as before). With ``folders``, each
    folder is listed once per deploy and refs are matched by exact item name
    in configured order (first folder wins). Only login items with a password
    are considered.

    Args:
        env_content: Raw environment file content
        secret_config: Secret configuration with adapter settings

    Returns:
        Resolved environment content with secrets replaced

    Raises:
        SecretResolutionError: If authentication fails or secret not found
    """
    # Parse the template: `$$` escapes are literals, `$REF` are secrets.
    # Ref placeholders keep their template position while being resolved.
    values, refs = split_env_template(env_content)

    # A literal-only template never contacts Bitwarden: no unlock, no sync.
    if not refs:
        return render_env(values)

    # Authenticate with Bitwarden
    session = os.getenv("BW_SESSION")
    if not session:
        password_env = secret_config.options.get("password_env")
        if not password_env:
            raise SecretResolutionError(
                "You need to set options.password_env to use the bitwarden adapter "
                "or set the BW_SESSION environment variable",
                adapter="bitwarden",
            )
        session = _signin(password_env)

    # ``bw list``/``bw get`` read the local cache, so refresh it once before
    # any lookup; a folder or item created server-side is otherwise invisible.
    _sync(session)

    folders = _folders_option(secret_config)
    if folders is None:
        resolved = _resolve_global(refs, session)
    else:
        resolved = _resolve_folders(refs, folders, session)

    values.update(resolved)

    return render_env(values)


def _folders_option(secret_config: SecretConfig) -> list[str] | None:
    """Return the configured folder lookup order, or ``None`` when unset.

    A single string is accepted as a one-element order. An empty list is
    rejected at config load; this guard keeps the adapter fail-closed for
    unvalidated configs too.
    """
    raw = secret_config.options.get("folders")
    if raw is None:
        return None
    folders = [raw] if isinstance(raw, str) else list(raw)
    if not folders:
        raise SecretResolutionError(
            "options.folders is empty; list at least one Bitwarden folder "
            "or remove the option for global name lookup",
            adapter="bitwarden",
        )
    return folders


def _resolve_global(refs: dict[str, str], session: str) -> dict[str, str]:
    """Resolve every ref by item name across the whole vault.

    One ``bw get password`` per ref, run concurrently. This is the legacy
    path, unchanged for configurations without ``folders``.
    """
    resolved_secrets = {}
    with ThreadPoolExecutor() as executor:
        future_to_key = {
            executor.submit(_read_secret, ref, session): key
            for key, ref in refs.items()
        }

        for future in as_completed(future_to_key):
            key = future_to_key[future]
            try:
                resolved_secrets[key] = future.result()
            except Exception as e:
                raise SecretResolutionError(
                    f"Failed to retrieve secret for {key}: {e}",
                    adapter="bitwarden",
                    key=key,
                ) from e

    return resolved_secrets


def _resolve_folders(
    refs: dict[str, str], folders: list[str], session: str
) -> dict[str, str]:
    """Resolve refs from folder item maps.

    Every configured folder is listed exactly once per invocation. Refs are
    matched by exact item name against the folders in configured order, so
    the first folder that contains the name wins. A ref containing a slash
    (``fujin/shared/postgres``) names its folder explicitly instead.
    """
    loader = _FolderLoader(session)
    # Eager: one API call per configured folder, regardless of ref count.
    for folder in folders:
        loader.items(folder)

    resolved: dict[str, str] = {}
    for key, ref in refs.items():
        folder, name = _split_ref(ref)
        if folder is not None:
            if not loader.has_folder(folder):
                raise _missing_secret(key, ref, [folder])
            items = loader.items(folder)
            if name not in items:
                raise _missing_secret(key, ref, [folder])
            resolved[key] = items[name]
            continue

        for candidate in folders:
            items = loader.items(candidate)
            if name in items:
                resolved[key] = items[name]
                break
        else:
            raise _missing_secret(key, ref, folders)

    return resolved


def _split_ref(ref: str) -> tuple[str | None, str]:
    """Split a folder-prefixed ref into ``(folder, item_name)``.

    ``fujin/shared/postgres`` -> ``("fujin/shared", "postgres")``. A ref
    without a usable folder prefix is returned as ``(None, ref)``.
    """
    folder, separator, name = ref.rpartition("/")
    if not separator or not folder or not name:
        return None, ref
    return folder, name


def _missing_secret(key: str, ref: str, folders: list[str]) -> SecretResolutionError:
    """Value-free failure naming the ref/key and the folders that were tried."""
    tried = ", ".join(f"'{folder}'" for folder in folders)
    return SecretResolutionError(
        f"Secret for {key} (ref '{ref}') was not found in Bitwarden folder(s): {tried}",
        adapter="bitwarden",
        key=key,
    )


class _FolderLoader:
    """Caches folder ids and item maps for the duration of one resolution.

    The vault folder index is fetched at most once; each folder's items are
    fetched at most once. Item maps hold only login passwords, keyed by exact
    item name (the first item with a given name in a folder wins).
    """

    def __init__(self, session: str):
        self._session = session
        self._folder_ids: dict[str, str] | None = None
        self._items: dict[str, dict[str, str]] = {}

    def _load_folder_ids(self) -> dict[str, str]:
        if self._folder_ids is None:
            raw = _run_bw(
                ["list", "folders", "--raw"], self._session, "list Bitwarden folders"
            )
            listing = _parse_json(raw, "Bitwarden folder list")
            folder_ids: dict[str, str] = {}
            for entry in listing:
                if not isinstance(entry, dict):
                    continue
                name = entry.get("name")
                folder_id = entry.get("id")
                if isinstance(name, str) and isinstance(folder_id, str):
                    if name in folder_ids:
                        # Name-based lookup would silently pick one of the
                        # two folders; fail closed instead. Folder names are
                        # not secret values, ids/items stay out of the error.
                        raise SecretResolutionError(
                            f"Bitwarden folder name '{name}' is duplicated; "
                            "folder-scoped resolution by name is ambiguous",
                            adapter="bitwarden",
                        )
                    folder_ids[name] = folder_id
            self._folder_ids = folder_ids
        return self._folder_ids

    def has_folder(self, folder: str) -> bool:
        return folder in self._load_folder_ids()

    def _id_for(self, folder: str) -> str:
        folder_id = self._load_folder_ids().get(folder)
        if folder_id is None:
            raise SecretResolutionError(
                f"Bitwarden folder '{folder}' was not found",
                adapter="bitwarden",
            )
        return folder_id

    def items(self, folder: str) -> dict[str, str]:
        cached = self._items.get(folder)
        if cached is not None:
            return cached

        folder_id = self._id_for(folder)
        raw = _run_bw(
            ["list", "items", "--folderid", folder_id, "--raw"],
            self._session,
            f"list items in Bitwarden folder '{folder}'",
        )
        data = _parse_json(raw, f"Bitwarden items in folder '{folder}'")

        items: dict[str, str] = {}
        for item in data:
            if not isinstance(item, dict):
                continue
            # Only login-password items resolve; notes/cards/identities are
            # skipped rather than failing the deploy.
            login = item.get("login")
            if not isinstance(login, dict):
                continue
            name = item.get("name")
            password = login.get("password")
            if isinstance(name, str) and isinstance(password, str):
                items.setdefault(name, password)

        self._items[folder] = items
        return items


def _sync(session: str) -> None:
    """Refresh the local vault cache before the first lookup.

    Called once per adapter invocation: ``bw list``/``bw get`` read the local
    cache, so a folder or item created server-side stays invisible until a
    sync. Failures are reported without echoing CLI output and never fall
    back to the possibly stale cache.
    """
    result = subprocess.run(
        ["bw", "sync", "--session", session, "--nointeraction"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SecretResolutionError("Bitwarden vault sync failed", adapter="bitwarden")


def _run_bw(args: list[str], session: str, action: str) -> str:
    """Run one ``bw`` command and return stdout.

    Failures are reported without echoing CLI output: it can contain vault
    data, and the adapter must keep errors value-free.
    """
    result = subprocess.run(
        ["bw", *args, "--session", session, "--nointeraction"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SecretResolutionError(f"Failed to {action}", adapter="bitwarden")
    return result.stdout


def _parse_json(raw: str, what: str) -> list:
    """Parse a ``bw --raw`` JSON array, value-free on malformed output."""
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise SecretResolutionError(
            f"Unexpected response while reading {what}",
            adapter="bitwarden",
        ) from e
    if not isinstance(data, list):
        raise SecretResolutionError(
            f"Unexpected response while reading {what}",
            adapter="bitwarden",
        )
    return data


def _read_secret(name: str, session: str) -> str:
    """Read a single secret from Bitwarden.

    Args:
        name: Secret name/identifier
        session: Bitwarden session token

    Returns:
        Secret value

    Raises:
        SecretResolutionError: If secret not found
    """
    result = subprocess.run(
        [
            "bw",
            "get",
            "password",
            name,
            "--raw",
            "--session",
            session,
            "--nointeraction",
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        raise SecretResolutionError(
            f"Password not found for {name}", adapter="bitwarden", key=name
        )

    return result.stdout.strip()


def _signin(password_env: str) -> str:
    """Unlock Bitwarden vault and return session token.

    Args:
        password_env: Name of environment variable containing vault password

    Returns:
        Bitwarden session token

    Raises:
        SecretResolutionError: If authentication fails
    """
    unlock_result = subprocess.run(
        [
            "bw",
            "unlock",
            "--nointeraction",
            "--passwordenv",
            password_env,
            "--raw",
        ],
        capture_output=True,
        text=True,
    )

    if unlock_result.returncode != 0:
        raise SecretResolutionError(
            f"Bitwarden unlock failed: {unlock_result.stderr}", adapter="bitwarden"
        )

    return unlock_result.stdout.strip()
