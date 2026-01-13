from __future__ import annotations

from typing import Any
import cappa


class FujinError(cappa.Exit):
    """Base class for all Fujin errors that exit the program."""

    def __init__(self, message: str, code: int = 1, **kwargs: Any):
        super().__init__(message, code=code, **kwargs)


class ConnectionError(FujinError):
    """SSH connection and communication errors."""


class DeploymentError(FujinError):
    """Base class for deployment-related errors."""


class BuildError(DeploymentError):
    """Build command failed."""

    def __init__(
        self, message: str, command: str | None = None, code: int = 1, **kwargs: Any
    ):
        super().__init__(message, code=code, **kwargs)
        self.command = command


class UploadError(DeploymentError):
    """Bundle upload failed."""

    def __init__(
        self,
        message: str,
        checksum_mismatch: bool = False,
        code: int = 1,
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
        code: int = 1,
        **kwargs: Any,
    ):
        super().__init__(message, code=code, **kwargs)
        self.adapter = adapter
        self.key = key


class SSHKeyError(FujinError):
    """SSH key generation or copying failed."""


class SSHAuthenticationError(FujinError):
    """SSH authentication failed."""


class ImproperlyConfiguredError(FujinError):
    """Fujin is improperly configured."""


class ServiceDiscoveryError(FujinError):
    """Raised when service discovery fails."""
