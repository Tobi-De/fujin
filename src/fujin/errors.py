from __future__ import annotations

from typing import Any

import cappa

from fujin.exit_codes import (
    EXIT_DEPLOY_FAILED,
    EXIT_HOST_UNREACHABLE,
    EXIT_USER_ABORTED,
)


class FujinError(cappa.Exit):
    """Base class for all Fujin errors that exit the program."""

    def __init__(self, message: str, code: int = EXIT_USER_ABORTED, **kwargs: Any):
        super().__init__(message, code=code, **kwargs)


class DeploymentError(FujinError):
    """Base class for deployment-related errors (nothing was activated)."""

    def __init__(self, message: str, code: int = EXIT_DEPLOY_FAILED, **kwargs: Any):
        super().__init__(message, code=code, **kwargs)


class BuildError(DeploymentError):
    """Build command failed."""

    def __init__(
        self,
        message: str,
        command: str | None = None,
        code: int = EXIT_DEPLOY_FAILED,
        **kwargs: Any,
    ):
        super().__init__(message, code=code, **kwargs)
        self.command = command


class UploadError(DeploymentError):
    """Bundle upload failed."""

    def __init__(
        self,
        message: str,
        checksum_mismatch: bool = False,
        code: int = EXIT_DEPLOY_FAILED,
        **kwargs: Any,
    ):
        super().__init__(message, code=code, **kwargs)
        self.checksum_mismatch = checksum_mismatch


class SecretResolutionError(FujinError):
    """Failed to resolve secrets from external sources."""

    def __init__(
        self,
        message: str,
        adapter: str | None = None,
        key: str | None = None,
        code: int = EXIT_DEPLOY_FAILED,
        **kwargs: Any,
    ):
        super().__init__(message, code=code, **kwargs)
        self.adapter = adapter
        self.key = key


class SSHKeyError(FujinError):
    """SSH key generation or copying failed (user error)."""


class SSHAuthenticationError(FujinError):
    """SSH authentication failed (host/credential problem)."""

    def __init__(self, message: str, code: int = EXIT_HOST_UNREACHABLE, **kwargs: Any):
        super().__init__(message, code=code, **kwargs)


class ImproperlyConfiguredError(FujinError):
    """Fujin is improperly configured (user error)."""


class ServiceDiscoveryError(FujinError):
    """Raised when service discovery fails."""


class ConnectionError(FujinError):
    """SSH connection and communication errors (host unreachable)."""

    def __init__(self, message: str, code: int = EXIT_HOST_UNREACHABLE, **kwargs: Any):
        super().__init__(message, code=code, **kwargs)


class CommandError(FujinError):
    """SSH command execution errors."""
