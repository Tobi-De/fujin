"""Exit-code taxonomy: error classes and CLI usage errors."""

from __future__ import annotations

import pytest

import cappa

from fujin.__main__ import Fujin
from fujin.errors import (
    BuildError,
    CommandError,
    ConnectionError,
    DeploymentError,
    FujinError,
    ImproperlyConfiguredError,
    SecretResolutionError,
    ServiceDiscoveryError,
    SSHAuthenticationError,
    SSHKeyError,
    UploadError,
)
from fujin.exit_codes import (
    EXIT_DEPLOY_FAILED,
    EXIT_HOST_UNREACHABLE,
    EXIT_USAGE,
    EXIT_USER_ABORTED,
)
from fujin.transfer import (
    HostUnreachableError,
    TransferError,
)

# ===========================================================================
# Error class -> exit code mapping
# ===========================================================================


@pytest.mark.parametrize(
    "error_class",
    [
        FujinError,
        ImproperlyConfiguredError,
        SSHKeyError,
        ServiceDiscoveryError,
    ],
)
def test_user_error_classes_exit_1(error_class):
    assert error_class("boom").code == EXIT_USER_ABORTED


@pytest.mark.parametrize(
    "error_class",
    [
        DeploymentError,
        BuildError,
        UploadError,
        SecretResolutionError,
        TransferError,
    ],
)
def test_deployment_error_classes_exit_4(error_class):
    assert error_class("boom").code == EXIT_DEPLOY_FAILED


@pytest.mark.parametrize(
    "error_class",
    [
        ConnectionError,
        SSHAuthenticationError,
        HostUnreachableError,
    ],
)
def test_host_error_classes_exit_5(error_class):
    assert error_class("boom").code == EXIT_HOST_UNREACHABLE


def test_explicit_code_overrides_class_default():
    assert DeploymentError("boom", code=EXIT_USER_ABORTED).code == EXIT_USER_ABORTED
    # conn.run classifies a remote command's exit status into CommandError.
    assert CommandError("boom", code=7).code == 7


def test_command_error_defaults_to_user_error():
    assert CommandError("boom").code == EXIT_USER_ABORTED


# ===========================================================================
# Command-level classification
# ===========================================================================


def test_showenv_secret_resolution_failure_exits_1(monkeypatch, minimal_config):
    """`fujin showenv` resolves secrets locally: a missing secret is a user
    configuration error (1), never a failed deploy (4)."""
    import fujin.commands.showenv as showenv_mod
    from fujin.commands.showenv import Showenv

    cmd = Showenv()
    cmd.config = minimal_config

    def boom(*_args, **_kwargs):
        raise SecretResolutionError(
            "Secret TOKEN not found", adapter="system", key="TOKEN"
        )

    monkeypatch.setattr(showenv_mod, "resolve_secrets", boom)

    with pytest.raises(SystemExit) as excinfo:
        cmd()

    assert excinfo.value.code == EXIT_USER_ABORTED


# ===========================================================================
# cappa usage errors
# ===========================================================================


def test_cli_usage_error_exits_2():
    with pytest.raises(SystemExit) as excinfo:
        cappa.invoke(
            Fujin,
            argv=["deploy", "--definitely-not-an-option"],
            version="0.0.0",
        )
    assert excinfo.value.code == EXIT_USAGE
