from __future__ import annotations

from dataclasses import dataclass

import cappa

from fujin.commands import BaseCommand
from fujin.build import clean_build_cache


@cappa.command(
    help="Manage the local build cache (.fujin/cache/bundles)",
)
@dataclass
class Cache(BaseCommand):
    @cappa.command(help="Remove cached build bundles")
    def clean(self):
        removed = clean_build_cache(self.config)
        if removed:
            self.output.success(f"Removed {removed} cached bundle(s).")
        else:
            self.output.info("Build cache is empty; nothing to remove.")
