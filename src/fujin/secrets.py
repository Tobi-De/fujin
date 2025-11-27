from __future__ import annotations

import os
import subprocess
from contextlib import closing
from contextlib import contextmanager
from io import StringIO
from typing import Callable, ContextManager
from typing import Generator

import cappa
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import dotenv_values

from fujin.config import SecretAdapter
from fujin.config import SecretConfig

secret_reader = Callable[[str], str]
secret_adapter_context = Callable[[SecretConfig], ContextManager[secret_reader]]


def resolve_secrets(env_content: str, secret_config: SecretConfig) -> str:
    adapter_to_context: dict[SecretAdapter, secret_adapter_context] = {
        SecretAdapter.SYSTEM: system,
        SecretAdapter.BITWARDEN: bitwarden,
        SecretAdapter.ONE_PASSWORD: one_password,
        SecretAdapter.DOPPLER: doppler,
    }
    if not env_content:
        return ""
    with closing(StringIO(env_content)) as buffer:
        env_dict = dotenv_values(stream=buffer)
    secrets = {key: value for key, value in env_dict.items() if value.startswith("$")}
    if not secrets:
        return env_content
    adapter_context = adapter_to_context[secret_config.adapter]
    parsed_secrets = {}
    with adapter_context(secret_config) as reader, ThreadPoolExecutor() as executor:
        future_to_key = {
            executor.submit(reader, secret[1:]): key for key, secret in secrets.items()
        }
        for future in as_completed(future_to_key):
            key = future_to_key[future]
            try:
                parsed_secrets[key] = future.result()
            except Exception as e:
                raise cappa.Exit(f"Failed to retrieve secret for {key}: {e}") from e

    env_dict.update(parsed_secrets)
    return "\n".join(f'{key}="{value}"' for key, value in env_dict.items())


# =============================================================================================
# BITWARDEN
# =============================================================================================


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


# =============================================================================================
# SYSTEM
# =============================================================================================


@contextmanager
def system(_: SecretConfig) -> Generator[secret_reader, None, None]:
    try:
        yield os.getenv
    finally:
        pass


# =============================================================================================
# ONE_PASSWORD
# =============================================================================================


@contextmanager
def one_password(_: SecretConfig) -> Generator[secret_reader, None, None]:
    def read_secret(name: str) -> str:
        result = subprocess.run(["op", "read", name], capture_output=True, text=True)
        if result.returncode != 0:
            raise cappa.Exit(result.stderr)
        return result.stdout.strip()

    try:
        yield read_secret
    finally:
        pass


# =============================================================================================
# DOPPLER
# =============================================================================================


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
