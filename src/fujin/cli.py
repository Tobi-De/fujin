"""Shared helpers for the CLI entry points."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager

from fujin.errors import CommandInterrupted
from fujin.exit_codes import EXIT_USER_ABORTED


@contextmanager
def quiet_interrupts() -> Iterator[None]:
    """Stop on Ctrl+C without dumping a traceback.

    Pressing Ctrl+C is a normal way to end a long remote command, not a bug.
    Both the local ``KeyboardInterrupt`` and the :class:`CommandInterrupted`
    raised by the raw-mode loop turn into the documented "user abort" exit
    code, with the shell prompt starting on a fresh line.
    """
    try:
        yield
    except (KeyboardInterrupt, CommandInterrupted):
        sys.stderr.write("\n")
        sys.stderr.flush()
        raise SystemExit(EXIT_USER_ABORTED) from None
