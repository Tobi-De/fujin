"""Doppler secret adapter for Fujin."""

from __future__ import annotations

import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from io import StringIO

from dotenv import dotenv_values
from fujin.config import SecretConfig
from fujin.errors import SecretResolutionError


def doppler(env_content: str, secret_config: SecretConfig) -> str:
    """Doppler secret adapter.

    Retrieves secrets from Doppler using the `doppler` CLI.
    Handles concurrent resolution of multiple secrets for performance.

    Requires `doppler setup` to be run in project root first.

    Configuration:
        adapter = "doppler"

    Args:
        env_content: Raw environment file content
        secret_config: Secret configuration (unused for doppler)

    Returns:
        Resolved environment content with secrets replaced

    Raises:
        SecretResolutionError: If secret not found or CLI fails
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

    # Resolve secrets concurrently
    resolved_secrets = {}
    with ThreadPoolExecutor() as executor:
        future_to_key = {
            executor.submit(_read_secret, secret[1:]): key
            for key, secret in secrets.items()
        }

        for future in as_completed(future_to_key):
            key = future_to_key[future]
            try:
                resolved_secrets[key] = future.result()
            except Exception as e:
                raise SecretResolutionError(
                    f"Failed to retrieve secret for {key}: {e}",
                    adapter="doppler",
                    key=key,
                ) from e

    # Merge resolved secrets back into env dict
    env_dict.update(resolved_secrets)

    return "\n".join(f'{key}="{value}"' for key, value in env_dict.items())


def _read_secret(name: str) -> str:
    """Read a single secret from Doppler.

    Args:
        name: Secret/variable name

    Returns:
        Secret value

    Raises:
        SecretResolutionError: If secret not found
    """
    result = subprocess.run(
        ["doppler", "run", "--command", f"echo ${name}"],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        raise SecretResolutionError(result.stderr, adapter="doppler", key=name)

    return result.stdout.strip()
