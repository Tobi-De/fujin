from __future__ import annotations

import cappa

from fujin.commands import BaseCommand
import webbrowser


@cappa.command(help="Configuration documentation")
class Docs(BaseCommand):
    def __call__(self):
        webbrowser.open(
            "https://fujin.falcoproject.com/en/latest/configuration.html", new=2
        )
