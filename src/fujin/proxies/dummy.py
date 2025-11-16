from __future__ import annotations

from pathlib import Path

from fujin.config import Config
from fujin.connection import Connection


class WebProxy:
    config_file: Path

    @classmethod
    def create(cls, *args, **kwargs) -> WebProxy:
        return cls()

    def install(self):
        pass

    def uninstall(self):
        pass

    def setup(self):
        pass

    def teardown(self):
        pass

    def export_config(self):
        pass
