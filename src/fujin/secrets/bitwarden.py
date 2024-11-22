from __future__ import annotations

import os
import subprocess
from contextlib import contextmanager
from typing import Generator
from typing import TYPE_CHECKING

import cappa

from fujin.config import SecretConfig

if TYPE_CHECKING:
    from . import secret_reader


@contextmanager
def bitwarden(secret_config: SecretConfig) -> Generator[secret_reader, None, None]:
    session = os.getenv("BW_SESSION")
    if not session:
        if not secret_config.password_env:
            raise cappa.Exit(
                "You need to set the password_env to use the bitwarden adapter or set the BW_SESSION environment variable",
                code=1,
            )
        session = _signin(secret_config.password_env)

    def read_secret(name: str) -> str:
        result = subprocess.run(
            [
                "bw",
                "get",
                "password",
                name,
                "--raw",
                "--session",
                session,
                "--nointeraction",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise cappa.Exit(f"Password not found for {name}")
        return result.stdout.strip()

    try:
        yield read_secret
    finally:
        pass
        # subprocess.run(["bw", "lock"], capture_output=True)


def _signin(password_env) -> str:
    sync_result = subprocess.run(["bw", "sync"], capture_output=True, text=True)
    if sync_result.returncode != 0:
        raise cappa.Exit(f"Bitwarden sync failed: {sync_result.stdout}", code=1)
    unlock_result = subprocess.run(
        [
            "bw",
            "unlock",
            "--nointeraction",
            "--passwordenv",
            password_env,
            "--raw",
        ],
        capture_output=True,
        text=True,
    )
    if unlock_result.returncode != 0:
        raise cappa.Exit(f"Bitwarden unlock failed {unlock_result.stderr}", code=1)

    return unlock_result.stdout.strip()
