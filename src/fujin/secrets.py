from __future__ import annotations

import logging
import os
from contextlib import closing
from importlib.metadata import entry_points
from io import StringIO
from typing import Callable

from dotenv import dotenv_values

from fujin.config import SecretConfig
from fujin.errors import SecretResolutionError

secret_adapter = Callable[[str, SecretConfig], str]

_adapter_registry: dict[str, secret_adapter] | None = None


def _discover_adapters() -> dict[str, secret_adapter]:
    """Discover and register all secret adapters (built-in + plugins)."""

    adapters: dict[str, secret_adapter] = {}

    adapters["system"] = system
    plugin_eps = entry_points(group="fujin.secrets")

    for ep in plugin_eps:
        try:
            adapter_func = ep.load()
            adapters[ep.name] = adapter_func
        except Exception as e:
            # Log warning but continue - don't fail on broken plugins
            logging.getLogger(__name__).warning(
                f"Failed to load secret adapter plugin '{ep.name}': {e}"
            )

    return adapters


def get_adapter_registry() -> dict[str, secret_adapter]:
    """Get the adapter registry, initializing it if needed."""
    global _adapter_registry
    if _adapter_registry is None:
        _adapter_registry = _discover_adapters()
    return _adapter_registry


def resolve_secrets(env_content: str, secret_config: SecretConfig) -> str:
    if not env_content:
        return ""

    adapter_registry = get_adapter_registry()
    adapter_name = secret_config.adapter

    if adapter_name not in adapter_registry:
        available = ", ".join(sorted(adapter_registry.keys()))
        raise SecretResolutionError(
            f"Secret adapter '{adapter_name}' is not installed.\n\n"
            f"Available adapters: {available}\n\n"
            f"To use '{adapter_name}', install the plugin:\n"
            f"  uv pip install fujin-secrets-{adapter_name}",
            adapter=adapter_name,
        )

    adapter_func = adapter_registry[adapter_name]
    try:
        return adapter_func(env_content, secret_config)
    except Exception as e:
        raise SecretResolutionError(
            f"Adapter '{adapter_name}' failed: {e}", adapter=adapter_name
        ) from e


# =============================================================================================
# SYSTEM ADAPTER (Built-in)
# =============================================================================================


def system(env_content: str, secret_config: SecretConfig) -> str:
    """
    Built-in adapter that reads secrets from environment variables.
    """
    with closing(StringIO(env_content)) as buffer:
        env_dict = dotenv_values(stream=buffer)

    # Resolve secrets (values starting with $)
    for key, value in env_dict.items():
        if value and value.startswith("$"):
            secret_name = value[1:]  # Strip $
            resolved = os.getenv(secret_name)
            if resolved is not None:
                env_dict[key] = resolved

    # Format env vars - only quote values that need it
    lines = []
    for key, value in env_dict.items():
        if value is None:
            lines.append(f"{key}=")
        elif _needs_quotes(value):
            lines.append(f'{key}="{value}"')
        else:
            lines.append(f"{key}={value}")
    return "\n".join(lines)


def _needs_quotes(value: str) -> bool:
    """Check if a value needs to be quoted in env file."""
    if not value:
        return False
    # Quote if contains spaces, special chars, or starts with quote
    special_chars = {" ", "\t", "#", "$", "\\", '"', "'", "`", "&", "|", ";", "<", ">"}
    return any(char in value for char in special_chars)
