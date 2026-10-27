from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
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


# Placeholder that stands in for an escaped ``$$`` while dotenv interpolates.
# It must contain no ``$`` so python-dotenv's ``${VAR}`` expansion cannot see
# an escaped dollar, and ``\x00`` keeps it out of the way of real content.
_DOLLAR_SENTINEL = "\x00fujin-dollar\x00"


@dataclass(frozen=True)
class EnvEntry:
    """One env file entry.

    Exactly one of ``literal``/``ref`` carries the value: ``ref`` is set for a
    ``$REF`` secret reference, ``literal`` for everything else, already
    unescaped. A bare key (``KEY=``) has both ``literal`` and ``ref`` set to
    ``None``.
    """

    key: str
    literal: str | None = None
    ref: str | None = None


def parse_env_template(env_content: str) -> list[EnvEntry]:
    """Parse env file content into typed entries.

    This is the single template parser used by every adapter and command:

    * ``$$`` escapes a literal ``$`` and is unescaped globally within the
      value, checked *before* secret-ref detection (it is swapped for a
      sentinel before dotenv sees the buffer).
    * A value starting with a single ``$`` is a secret reference; the rest of
      the value (to end of line) is the ref.
    * Everything else goes through python-dotenv's normal interpolation, so
      ``${VAR}`` expands exactly as it always has for env files without
      ``$$`` escapes.
    """
    entries: list[EnvEntry] = []
    with closing(StringIO(env_content.replace("$$", _DOLLAR_SENTINEL))) as buffer:
        parsed = dotenv_values(stream=buffer)
    for key, value in parsed.items():
        if value is None:
            entries.append(EnvEntry(key))
        elif value.startswith("$") and len(value) > 1:
            entries.append(EnvEntry(key, ref=value[1:].replace(_DOLLAR_SENTINEL, "$")))
        else:
            entries.append(EnvEntry(key, literal=value.replace(_DOLLAR_SENTINEL, "$")))
    return entries


def render_env(values: Mapping[str, str | None]) -> str:
    """Render env vars as a file - only quote values that need it."""
    lines = []
    for key, value in values.items():
        if value is None:
            lines.append(f"{key}=")
        elif _needs_quotes(value):
            lines.append(f'{key}="{value}"')
        else:
            lines.append(f"{key}={value}")
    return "\n".join(lines)


def split_env_template(
    env_content: str,
) -> tuple[dict[str, str | None], dict[str, str]]:
    """Split a template into ``(values, refs)``.

    ``values`` maps every key to its literal value (``None`` for a bare key)
    in template order; ref keys are present with ``None``. ``refs`` maps
    only the ``$REF`` keys to their ref.
    """
    values: dict[str, str | None] = {}
    refs: dict[str, str] = {}
    for entry in parse_env_template(env_content):
        if entry.ref is None:
            values[entry.key] = entry.literal
        else:
            values[entry.key] = None
            refs[entry.key] = entry.ref
    return values, refs


def render_template(env_content: str) -> str:
    """Render a template as env file text with ``$REF`` values left intact.

    ``$$`` escapes are unescaped and ``$REF`` values are written back as
    literal ``$REF`` in template order - used when there is no secret adapter
    to resolve them (``show env``, adapter-less deploys).
    """
    values, refs = split_env_template(env_content)
    for key, ref in refs.items():
        values[key] = f"${ref}"
    return render_env(values)


def secret_refs(env_content: str) -> dict[str, str]:
    """Return a mapping of env var key -> secret ref for ``$``-prefixed values.

    ``API_KEY=$MY_SECRET`` yields ``{"API_KEY": "MY_SECRET"}``. Escaped
    ``$$...`` values are literals and are omitted.
    """
    return split_env_template(env_content)[1]


def env_values(env_content: str) -> dict[str, str]:
    """Parse an env file (resolved or not) into ``{key: value}``."""
    with closing(StringIO(env_content)) as buffer:
        return {k: v for k, v in dotenv_values(stream=buffer).items() if v is not None}


def has_secret_refs(env_content: str) -> bool:
    """True when the env file contains any ``$REF`` secret reference (#75)."""
    return bool(secret_refs(env_content))


def resolve_secrets(env_content: str, secret_config: SecretConfig) -> str:
    if not env_content:
        return ""

    # No $-refs: no adapter needs to be contacted (#75 staging case - a host
    # with only literal values must not invoke a global adapter at all).
    # Still go through the shared parser so `$$` escapes are unescaped.
    if not has_secret_refs(env_content):
        return render_env(split_env_template(env_content)[0])

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
        # FujinError (incl. SecretResolutionError) carries its text on
        # `.message`; `str()` would yield only the exit code.
        detail = str(getattr(e, "message", e))
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
    values, refs = split_env_template(env_content)
    for key, ref in refs.items():
        resolved = os.getenv(ref)
        if resolved is None:
            raise SecretResolutionError(
                f"Secret reference '<${ref}>' for '{key}' is not set"
                " in the environment.",
                adapter="system",
                key=key,
            )
        values[key] = resolved

    return render_env(values)


def _needs_quotes(value: str) -> bool:
    """Check if a value needs to be quoted in env file."""
    if not value:
        return False
    # Quote if contains spaces, special chars, or starts with quote
    special_chars = {" ", "\t", "#", "$", "\\", '"', "'", "`", "&", "|", ";", "<", ">"}
    return any(char in value for char in special_chars)
