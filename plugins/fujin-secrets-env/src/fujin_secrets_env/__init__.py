"""Environment variable secret adapter for Fujin."""

from __future__ import annotations

import json
import os
from contextlib import closing
from io import StringIO

from dotenv import dotenv_values

from fujin.config import SecretConfig
from fujin.errors import SecretResolutionError


def env(env_content: str, secret_config: SecretConfig) -> str:
    """Environment variable secret adapter.

    Reads secrets from a JSON-formatted environment variable. Useful for CI
    systems like GitHub Actions where secrets can be passed as JSON.

    Configuration:
        [secrets]
        adapter = "env"
        source = "FUJIN_SECRETS"  # Name of env var containing JSON secrets

    Example GitHub Actions usage:
        env:
          FUJIN_SECRETS: ${{ toJSON(secrets) }}

    Args:
        env_content: Raw environment file content
        secret_config: Secret configuration with adapter settings

    Returns:
        Resolved environment content with secrets replaced

    Raises:
        SecretResolutionError: If source env var not set or invalid JSON
    """
    source = secret_config.options.get("source")
    if not source:
        raise SecretResolutionError(
            "The 'options.source' parameter is required for the env adapter. "
            "Set it to the name of the environment variable containing secrets JSON.",
            adapter="env",
        )

    # Read JSON from source env var
    secrets_json = os.getenv(source)
    if not secrets_json:
        raise SecretResolutionError(
            f"Environment variable '{source}' is not set or empty.",
            adapter="env",
        )

    try:
        secrets = json.loads(secrets_json)
    except json.JSONDecodeError as e:
        raise SecretResolutionError(
            f"Invalid JSON in '{source}': {e}",
            adapter="env",
        ) from e

    if not isinstance(secrets, dict):
        raise SecretResolutionError(
            f"'{source}' must contain a JSON object, got {type(secrets).__name__}",
            adapter="env",
        )

    # Parse env file
    with closing(StringIO(env_content)) as buffer:
        env_dict = dotenv_values(stream=buffer)

    # Identify secrets (values starting with $)
    secret_refs = {
        key: value[1:]  # Strip the $ prefix
        for key, value in env_dict.items()
        if value and value.startswith("$")
    }

    if not secret_refs:
        return env_content

    # Resolve secrets
    for key, secret_name in secret_refs.items():
        if secret_name not in secrets:
            raise SecretResolutionError(
                f"Secret '{secret_name}' not found in '{source}'",
                adapter="env",
                key=key,
            )
        env_dict[key] = secrets[secret_name]

    return "\n".join(f'{key}="{value}"' for key, value in env_dict.items())
