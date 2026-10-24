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


def secret_refs(env_content: str) -> dict[str, str]:
    """Return a mapping of env var key -> secret ref for ``$``-prefixed values.

    ``API_KEY=$MY_SECRET`` yields ``{"API_KEY": "MY_SECRET"}``. Values that
    do not start with ``$`` are not secrets and are omitted. Escaped
    ``$$...`` values are treated as literals and omitted.
    """
    refs: dict[str, str] = {}
    for line in env_content.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        stripped = value.strip().strip("'\"")
        if stripped.startswith("$$"):
            continue
        if stripped.startswith("$") and len(stripped) > 1:
            refs[key.strip()] = stripped[1:]
    return refs


def env_values(env_content: str) -> dict[str, str]:
    """Parse an env file (resolved or not) into ``{key: value}``."""
    with closing(StringIO(env_content)) as buffer:
        return {k: v for k, v in dotenv_values(stream=buffer).items() if v is not None}


def has_secret_refs(env_content: str) -> bool:
    """True when the env file contains any ``$REF`` secret reference (#75)."""
    return bool(secret_refs(env_content))


def _format_env(env_dict: dict[str, str | None]) -> str:
    """Format env vars - only quote values that need it."""
    lines = []
    for key, value in env_dict.items():
        if value is None:
            lines.append(f"{key}=")
        elif _needs_quotes(value):
            lines.append(f'{key}="{value}"')
        else:
            lines.append(f"{key}={value}")
    return "\n".join(lines)


def resolve_secrets(env_content: str, secret_config: SecretConfig) -> str:
    if not env_content:
        return ""

    # No $-refs: no adapter needs to be contacted (#75 staging case - a host
    # with only literal values must not invoke a global adapter at all).
    # Reuse the system adapter's formatting for output parity.
    if not has_secret_refs(env_content):
        with closing(StringIO(env_content)) as buffer:
            return _format_env(dict(dotenv_values(stream=buffer)))

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
        # Never echo the env buffer in an adapter failure: an adapter's
        # message may quote the file it was working on, and the buffer is
        # only meaningful locally. Raw secret values are not in it yet
        # (resolution failed), but the refs must not leak either.
        detail = str(e)
        for secret_line in env_content.splitlines():
            if secret_line and secret_line in detail:
                detail = detail.replace(secret_line, "").replace(
                    secret_line.strip(), ""
                )
        raise SecretResolutionError(
            f"Adapter '{adapter_name}' failed: {detail.strip()}", adapter=adapter_name
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
    return _format_env(env_dict)


def _needs_quotes(value: str) -> bool:
    """Check if a value needs to be quoted in env file."""
    if not value:
        return False
    # Quote if contains spaces, special chars, or starts with quote
    special_chars = {" ", "\t", "#", "$", "\\", '"', "'", "`", "&", "|", ";", "<", ">"}
    return any(char in value for char in special_chars)
