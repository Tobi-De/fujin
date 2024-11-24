from __future__ import annotations

import subprocess
from contextlib import contextmanager
from typing import Generator
from typing import TYPE_CHECKING

import cappa

from fujin.config import SecretConfig

if TYPE_CHECKING:
    from . import secret_reader


@contextmanager
def doppler(_: SecretConfig) -> Generator[secret_reader, None, None]:
    def read_secret(name: str) -> str:
        result = subprocess.run(
            ["doppler", "run", "--command", f"echo ${name}"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise cappa.Exit(result.stderr)
        return result.stdout.strip()

    try:
        yield read_secret
    finally:
        pass
