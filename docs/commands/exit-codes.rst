Exit Codes
==========

Fujin uses a stable exit-code taxonomy so scripts and CI pipelines can react
to failures without parsing output. The codes are part of the public
interface.

.. list-table::
   :header-rows: 1
   :widths: 10 35 55

   * - Code
     - Meaning
     - Phase / raised by
   * - ``0``
     - Success
     - Any command that completed normally. ``fujin rollback --to`` a release
       that is already current, or an interactive rollback with nothing to do,
       also exits ``0``.
   * - ``1``
     - User error or abort
     - Invalid configuration, an invalid/unknown/ambiguous rollback target, a
       declined confirmation prompt, a keyboard interrupt, or an unclassified
       :class:`fujin.errors.FujinError`. A ``fujin showenv`` secret that
       cannot be resolved is also ``1``: nothing is deployed, so it is a local
       configuration error. Declining the rollback prompt after a
       service-start failure is also ``1``.
   * - ``2``
     - CLI usage error
     - Emitted by the argument parser (cappa) for unknown commands, arguments
       or options, before any deployment work happens.
   * - ``3``
     - Deploy failed, previous release restored
     - A deployed unit failed to start or become ready, and the automatic
       rollback to the previous release completed successfully.
   * - ``4``
     - Deploy failed, nothing rolled back
     - Build, upload, verification, secret-resolution or ``pre_install`` /
       ``post_install`` hook failure; a service-start failure with
       ``--no-rollback``; or a service-start failure with no previous release
       to roll back to. The new release may or may not be live (see below).
   * - ``5``
     - Host unreachable
     - The server could not be reached (connection refused/timeout, DNS
       failure, rsync exit 255), SSH authentication failed, or the SSH
       transport was lost mid-command (socket/libssh2 failure, a wedged
       channel that stops making progress, or EOF without a remote exit
       status). A failed staging cleanup never changes this code.
   * - ``6``
     - Rollback failed
     - An explicit ``fujin rollback`` could not install, restart or converge
       the target release (including swapping the ``current`` symlink or
       recording the new state), or the automatic rollback itself failed (for
       example, the previous release's manifest is missing or its services do
       not restart). The server may be in a partially rolled-back state and
       needs manual inspection; Fujin never attempts a second rollback.

Hook and service failures are not the same
------------------------------------------

Where the failure happens determines whether anything was activated:

* A failing ``pre_install`` hook, or a failing ``post_install`` hook, exits
  ``4``. ``pre_install`` runs while the old release is still live.
  ``post_install`` runs after the release directory is committed but
  **before** the ``current`` symlink is swapped, so the old release is still
  live. Nothing was activated, hence no ``3`` and no ``6``.
* A failing ``post_start`` hook does **not** fail the deploy. It runs after
  the ``current`` symlink was swapped and the services passed their readiness
  checks, so the release is already live: Fujin logs a warning and exits
  ``0``, leaving the deployed release in place.
* A service that fails to start or never becomes ready after activation exits
  ``3`` when Fujin's automatic rollback restores the previous release, ``4``
  with ``--no-rollback`` (or when no previous release exists), and ``6`` if
  the rollback itself fails.

Commands that propagate remote exit statuses
--------------------------------------------

``fujin app exec`` and ``fujin server exec`` run an arbitrary remote command
and exit with **that command's** exit status. They are a transparent wrapper,
so they are intentionally outside this taxonomy: a remote status of ``4`` or
``5`` there means whatever the remote command says it means, not a deploy
failure or an unreachable host.

Dry runs
--------

``fujin deploy --dry-run`` previews the deployment plan without touching the
server or changing any state, and exits ``0`` when the preview succeeds. A
failed dry run still reports its real failure code from this taxonomy. (The
flag is delivered with the dry-run work in #90; this is the contract it must
honor.)

See :doc:`deploy` and :doc:`rollback` for the full command behavior.
