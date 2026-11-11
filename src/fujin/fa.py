"""Shortcut entry point for fujin app commands."""

from __future__ import annotations

import cappa

from fujin.cli import quiet_interrupts
from fujin.commands.app import App


def main():
    """Entry point for 'fa' command - shortcut to 'fujin app' subcommands."""
    with quiet_interrupts():
        cappa.invoke(App)


if __name__ == "__main__":
    main()
