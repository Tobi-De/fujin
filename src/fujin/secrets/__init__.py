from __future__ import annotations

from contextlib import closing
from io import StringIO
from typing import Callable, ContextManager

import gevent
from dotenv import dotenv_values

from fujin.config import SecretAdapter
from fujin.config import SecretConfig
from .bitwarden import bitwarden
from .dopppler import doppler
from .onepassword import one_password
from .system import system

secret_reader = Callable[[str], str]
secret_adapter_context = Callable[[SecretConfig], ContextManager[secret_reader]]

adapter_to_context: dict[SecretAdapter, secret_adapter_context] = {
    SecretAdapter.SYSTEM: system,
    SecretAdapter.BITWARDEN: bitwarden,
    SecretAdapter.ONE_PASSWORD: one_password,
    SecretAdapter.DOPPLER: doppler,
}


def resolve_secrets(env_content: str, secret_config: SecretConfig) -> str:
    if not env_content:  # this is really for empty string
        return ""
    with closing(StringIO(env_content)) as buffer:
        env_dict = dotenv_values(stream=buffer)
    secrets = {key: value for key, value in env_dict.items() if value.startswith("$")}
    if not secrets:
        return env_content
    adapter_context = adapter_to_context[secret_config.adapter]
    parsed_secrets = {}
    with adapter_context(secret_config) as reader:
        for key, secret in secrets.items():
            parsed_secrets[key] = gevent.spawn(
                reader, secret[1:]
            )  # remove the leading $
        gevent.joinall(parsed_secrets.values())
    env_dict.update({key: thread.value for key, thread in parsed_secrets.items()})
    return "\n".join(f'{key}="{value}"' for key, value in env_dict.items())
