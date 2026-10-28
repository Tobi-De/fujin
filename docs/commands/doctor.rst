doctor
======

Check server-state integrity and repair safe inconsistencies.

Overview
--------

``fujin doctor`` inspects the deployment state of an application on the
server and reports one line per check as ``OK``, ``WARN`` or ``FAIL``. The
default run is **read-only** - it never changes the server.

``--fix`` repairs the inconsistencies that can be fixed without guessing or
losing history. Problems that need a new deploy or a manual decision (a
missing release, a ``current`` symlink whose recorded release is gone) are
still only reported.

While a deploy may be running, ``--fix`` is deliberately inert: it never
rewrites state, deletes releases, reinstalls units, converges instances or
removes staging directories. The next plain run after the lock is gone
repairs whatever remains.

Usage
-----

.. code-block:: bash

   fujin doctor [OPTIONS]

Options
-------

``--fix``
   Repair safe problems instead of only reporting them. Default: off.

Checks
------

``state``
   Compares ``store/state.json`` with the ``current`` symlink. The symlink is
   the source of truth: when they disagree, ``--fix`` rewrites ``state.json``
   to match it, preserving the deploys history and turning the previously
   recorded release into the single-level rollback target (``previous``).
   A previous digest whose release directory is gone is dropped rather than
   recorded. When ``current`` is missing but ``state.json`` names a release
   that still exists on disk, ``--fix`` recreates the symlink atomically
   (``ln -sfn`` + ``mv -T``); if that release is gone, only a report is
   produced.

``releases``
   Reports releases referenced by state (``current``, ``previous``, deploy
   records) that are missing on disk, and release directories that no state
   entry references. ``--fix`` removes orphaned directories; the current and
   previous releases are never touched. If ``state.json`` is missing or
   corrupt, *no release is ever removed*: without it the rollback target is
   unknowable, so orphans are reported with a note only (the state check may
   still rewrite ``state.json``). Historical deploy records pruned by
   garbage collection are reported as ``WARN``, not as an error.

``units``
   Compares the unit files and dropins installed in ``/etc/systemd/system``
   with the live release's ``systemd/`` tree: drifted content, missing files
   and stray units - including stray dropin files inside a kept ``*.d``
   directory. A release that declares no units is compared against an empty
   expected set, so leftovers from a previous release are reported. ``--fix``
   re-installs the release's units, prunes strays (top-level and inside kept
   ``*.d`` dirs), then verifies the installed tree and reports what actually
   converged.

``instances``
   Compares enabled/active template instances with the ``@.service`` units
   declared by the live release (``replicas`` in ``fujin.toml`` sets the
   count; a replica key with no template unit is ignored, mirroring deploy).
   ``--fix`` starts missing instances and stops extra ones.

``permissions``
   Reports the actual modes of the app directory (setgid), the live release's
   ``.env`` (0640), the socket directory ``/run/{app}`` (group readable) and
   whether the ``caddy`` user is in the app's group. ``--fix`` applies the
   safe ``chmod`` fixes; group membership must be changed manually.

``caddy``
   Verifies that ``/etc/caddy/conf.d/{app}.caddy`` exists and that
   ``caddy validate`` accepts it. Skipped with a clear note when Caddy is not
   installed or the app has no Caddy configuration.

``lock``
   Inspects the deploy lock (``store/lock``, ``store/lock.holder`` and the
   legacy ``.deploy_lock``). A lock whose holder PID is alive is reported as
   running and never removed; a stale lock holder is removed by ``--fix``.

   A probe that finds the lock ``live``, ``held`` (flock held, no holder pid)
   or ``stale`` (dead holder pid) counts as a *deploy lock present*: every
   other repair is skipped for that run and the affected checks say
   ``deploy lock present; fixes skipped``. This keeps ``doctor --fix`` from
   racing or corrupting a deployment that is still starting, mid-transfer, or
   finishing. A stale lock is cleaned up by this same run, so the next run
   can repair.

``staging``
   Reports orphaned ``releases/.staging-*`` directories left by crashed
   deploys. ``--fix`` removes them only when the deploy lock is strictly
   ``free``; with any lock present they are reported as left untouched.

``audit``
   Cross-references release digests recorded in the server-side audit log
   with the releases present on disk. Deploy and rollback records now carry
   the digest they acted on; a digest that no longer exists is a ``FAIL``.
   Legacy records written before digests were logged are reported as a note,
   not as a failure.

Exit Codes
----------

``0``
   All checks passed, or every reported problem was fixed.

``1``
   Problems remain (re-run with ``--fix`` to repair the safe ones).

Examples
--------

**Report only**

.. code-block:: bash

   fujin doctor

**Repair safe problems**

.. code-block:: bash

   fujin doctor --fix

**Check a specific host**

.. code-block:: bash

   fujin doctor --host production

See Also
--------

- :doc:`deploy` - Deploy command
- :doc:`rollback` - Rollback command
- :doc:`audit` - Audit log viewer
- :doc:`exit-codes` - Exit-code taxonomy
