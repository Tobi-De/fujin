"""Bitwarden secret adapter for Fujin."""

from __future__ import annotations

import os
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from io import StringIO

from dotenv import dotenv_values
from fujin.config import SecretConfig
from fujin.errors import SecretResolutionError


def bitwarden(env_content: str, secret_config: SecretConfig) -> str:
    """Bitwarden secret adapter.

    Retrieves secrets from Bitwarden vault using the `bw` CLI.
    Handles concurrent resolution of multiple secrets for performance.

    Configuration:
        - Requires BW_SESSION environment variable, OR
        - Set password_env to unlock vault using environment variable

    Example fujin.toml:
        [secrets]
        adapter = "bitwarden"
        password_env = "BW_PASSWORD"  # Optional

    Args:
        env_content: Raw environment file content
        secret_config: Secret configuration with adapter settings

    Returns:
        Resolved environment content with secrets replaced

    Raises:
        SecretResolutionError: If authentication fails or secret not found
    """
    # Parse env file
    with closing(StringIO(env_content)) as buffer:
        env_dict = dotenv_values(stream=buffer)

    # Identify secrets (values starting with $)
    secrets = {
        key: value for key, value in env_dict.items() if value and value.startswith("$")
    }

    if not secrets:
        return env_content

    # Authenticate with Bitwarden
    session = os.getenv("BW_SESSION")
    if not session:
        if not secret_config.password_env:
            raise SecretResolutionError(
                "You need to set the password_env to use the bitwarden adapter "
                "or set the BW_SESSION environment variable",
                adapter="bitwarden",
            )
        session = _signin(secret_config.password_env)

    # Resolve secrets concurrently
    resolved_secrets = {}
    with ThreadPoolExecutor() as executor:
        future_to_key = {
            executor.submit(_read_secret, secret[1:], session): key
            for key, secret in secrets.items()
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

    # Merge resolved secrets back into env dict
    env_dict.update(resolved_secrets)

    return "\n".join(f'{key}="{value}"' for key, value in env_dict.items())


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
