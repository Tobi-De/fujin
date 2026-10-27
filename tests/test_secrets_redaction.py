"""Unit tests for the secrets redaction invariant (#87)."""

from __future__ import annotations

import logging as logmod

import pytest

from fujin import redaction as r
from fujin.config import SecretConfig
from fujin.errors import SecretResolutionError
from fujin.secrets import env_values, resolve_secrets, secret_refs

SENTINEL = "unit-test-sentinel-00"


@pytest.fixture(autouse=True)
def _registry_fresh():
    r.clear_secrets()
    yield
    r.clear_secrets()


def test_redact_replaces_registered_value_with_placeholder():
    r.register_secret(SENTINEL, "STRIPE_TOKEN")
    assert r.redact(f"boom {SENTINEL} done") == "boom <$STRIPE_TOKEN> done"
    assert r.redact("no secret here") == "no secret here"


def test_redact_longest_value_first():
    r.register_secret("hunter2", "PW")
    r.register_secret("hunter2x", "PW_X")
    text = "p hunter2x p hunter2"
    assert r.redact(text) == "p <$PW_X> p <$PW>"


def test_redact_skips_tiny_values():
    r.register_secret("ab", "SHORT")
    assert r.redact("xyz ab") == "xyz ab"


def test_clear_secrets_forgets_values():
    r.register_secret(SENTINEL, "REF")
    r.clear_secrets()
    assert r.redact(SENTINEL) == SENTINEL


def test_logging_filter_redacts_records():
    r.register_secret(SENTINEL, "REF")
    logger = logmod.getLogger("tests.secrets")
    seen: list[str] = []

    class _Sink(logmod.Handler):
        def emit(self, record):
            seen.append(self.format(record))

    sink = r.filtered(_Sink(level=logmod.INFO))
    logger.setLevel(logmod.INFO)
    logger.addHandler(sink)
    try:
        logger.info("value is %s", SENTINEL)
    finally:
        logger.removeHandler(sink)
    joined = "\n".join(seen)
    assert SENTINEL not in joined
    assert "<$REF>" in joined


def test_secret_refs_mapping():
    content = (
        "KEY=$REF_A\nKEY2=$REF_B\nLITERAL=plain\nCUDA=echo $$x\nESCAPED=$$NOT_A_REF\n"
    )
    # `$$...` values are literals and must not be registered/redacted as
    # secrets; only genuine `$REF` entries are.
    assert secret_refs(content) == {"KEY": "REF_A", "KEY2": "REF_B"}


def test_env_values_parses_resolved_file():
    content = 'A="hello world"\nB=plain\n'
    assert env_values(content) == {"A": "hello world", "B": "plain"}


def test_resolve_secrets_unknown_adapter_raises_before_upload(tmp_path):
    with pytest.raises(SecretResolutionError):
        resolve_secrets("K=$V", SecretConfig(adapter="definitely-not-installed"))


def test_adapter_failure_exposes_no_env_content(monkeypatch):
    def failing_adapter(env_content, secret_config):
        raise RuntimeError(f"adapter exploded near {env_content!r}")

    monkeypatch.setattr(
        "fujin.secrets.get_adapter_registry", lambda: {"test": failing_adapter}
    )
    with pytest.raises(SecretResolutionError) as e:
        resolve_secrets("KEY=$VERY_SECRET_REF", SecretConfig(adapter="test"))
    # The adapter's own message quoted the env buffer; the raised error must
    # not echo the secret line content.
    assert "VERY_SECRET_REF" not in e.value.message
