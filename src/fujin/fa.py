"""Shortcut entry point for fujin app commands."""

from __future__ import annotations

import cappa

import fujin
from fujin.commands.app import App


def main():
    """Entry point for 'fa' command - shortcut to 'fujin app' subcommands."""
    cappa.invoke(App, version=fujin.__version__)


if __name__ == "__main__":
    main()
