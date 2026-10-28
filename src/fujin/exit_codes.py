"""Documented process exit codes.

Fujin classifies every terminating condition into a stable, scriptable
exit-code taxonomy.  Codes are part of the public interface: scripts and
CI pipelines may rely on them.

===========  =====================================================
Code         Meaning / phase
===========  =====================================================
``0``        Success (``EXIT_OK``).
``1``        User error or abort (``EXIT_USER_ABORTED``): invalid
             configuration or target, a declined confirmation, or a
             user interrupt.  Also the default for unclassified
             :class:`fujin.errors.FujinError` subclasses.
``2``        CLI usage error (``EXIT_USAGE``); produced by cappa for
             unknown commands, arguments or options.
``3``        Deploy failed but the previous release was restored
             (``EXIT_DEPLOY_ROLLED_BACK``): a service failed to start
             and the automatic rollback succeeded.
``4``        Deploy failed before/without a successful rollback
             (``EXIT_DEPLOY_FAILED``): build, upload, verification or a
             ``pre_install``/``post_install`` hook failure, a failed
             service with ``--no-rollback``, or no previous release to
             roll back to. (A ``post_start`` hook failure only warns: the
             release is already live.)
``5``        Host unreachable (``EXIT_HOST_UNREACHABLE``): the server
             could not be reached, the SSH session/credentials were
             rejected, or the transport was lost mid-command (socket or
             libssh2 failure, wedged channel, EOF without an exit status).
``6``        Rollback failed (``EXIT_ROLLBACK_FAILED``): an explicit
             ``fujin rollback`` or the automatic rollback itself could
             not complete; the server may be in a partially rolled
             back state and needs manual inspection.
===========  =====================================================
"""

from __future__ import annotations

__all__ = [
    "EXIT_OK",
    "EXIT_USER_ABORTED",
    "EXIT_USAGE",
    "EXIT_DEPLOY_ROLLED_BACK",
    "EXIT_DEPLOY_FAILED",
    "EXIT_HOST_UNREACHABLE",
    "EXIT_ROLLBACK_FAILED",
]

#: Success.
EXIT_OK = 0

#: User error or abort (invalid target/configuration, declined prompt,
#: interrupt).
EXIT_USER_ABORTED = 1

#: CLI usage error; cappa emits this for unparsable arguments.
EXIT_USAGE = 2

#: Deploy failed and the previous release was restored successfully.
EXIT_DEPLOY_ROLLED_BACK = 3

#: Deploy failed and the previous release is not (knowingly) restored.
EXIT_DEPLOY_FAILED = 4

#: The host could not be reached, or SSH authentication failed.
EXIT_HOST_UNREACHABLE = 5

#: A rollback (explicit or automatic) failed to complete.
EXIT_ROLLBACK_FAILED = 6
