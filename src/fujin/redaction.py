"""Secret redaction for every fujin output path.

A resolved secret value must never appear in stdout/stderr, logs,
exceptions or audit output. Modules that handle secrets register the
resolved values here (see fujin.secrets); every place that shows text to
the user routes that text through :func:`redact`, which substitutes each
known secret value with a ``<$REF>`` placeholder.
"""

from __future__ import annotations

import logging
import threading

_lock = threading.Lock()

#: Resolved secret values registered for redaction: value -> placeholder.
_registered: dict[str, str] = {}

#: Secrets shorter than this are not registered (too easy to redact
#: false-positive substrings elsewhere in command output).
_MIN_SECRET_LEN = 4


def register_secret(value: str, ref: str) -> None:
    """Remember ``value`` so it is rendered as ``<${ref}>`` in output."""
    if not value or len(value) < _MIN_SECRET_LEN:
        return
    with _lock:
        _registered[value] = f"<${ref}>"


def register_secrets(values: dict[str, str], refs: dict[str, str]) -> None:
    """Bulk-register resolved values keyed by env key, mapped to refs."""
    for key, value in values.items():
        register_secret(value, refs.get(key, "SECRET"))


def clear_secrets() -> None:
    """Forget all registered secrets (fresh command invocation)."""
    with _lock:
        _registered.clear()


def redact(text: str) -> str:
    """Replace any registered secret value in ``text`` with its placeholder."""
    if not text:
        return text
    with _lock:
        if not _registered:
            return text
        items = sorted(_registered.items(), key=lambda kv: -len(kv[0]))
    for value, placeholder in items:
        text = text.replace(value, placeholder)
    return text


class RedactionFilter(logging.Filter):
    """Logging filter that scrubs registered secrets from log messages."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = None
        return True


def filtered(handler: logging.Handler) -> logging.Handler:
    """Return ``handler`` with the redaction filter attached."""
    handler.addFilter(RedactionFilter())
    return handler


def install_log_filter() -> None:
    """Attach the redaction filter to every root handler (idempotent)."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        already = any(isinstance(f, RedactionFilter) for f in handler.filters)
        if not already:
            handler.addFilter(RedactionFilter())
