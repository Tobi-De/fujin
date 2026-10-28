deploy
======

The ``fujin deploy`` command deploys your application to the server.

.. image:: ../_static/images/help/deploy-help.png
   :alt: fujin deploy command help
   :width: 100%

Overview
--------

This is the core deployment command. It builds your application locally, bundles all necessary files, uploads them to the server, and installs/configures everything.

Use ``fujin deploy`` for:

- Deploying code changes
- Updating configuration
- Updating environment variables
- Refreshing systemd units or Caddy configuration

How it works
------------

Here's a high-level overview of what happens when you run the ``deploy`` command:

1. **Resolve Secrets**: If you have a ``secrets`` configuration, Fujin retrieves secrets defined in your environment file from the configured adapter (Bitwarden, 1Password, Doppler, etc.).

2. **Build (with cache)**: Your application is built locally using the
   ``build_command`` from the configuration. The result is stored under
   ``.fujin/cache/bundles/<bundle_key>/``; a rebuild is skipped when the
   bundle key (build command, build env, requirements, unit/Caddyfile/asset
   sources, config fingerprint) is unchanged and the cached file hashes
   still verify. Opt out with ``--no-cache``.

3. **Assemble the Release Tree**: Fujin assembles the client-known tree
   (interpolated systemd unit files and dropins, Caddyfile, assets,
   requirements.txt) plus the rendered ``.env``, and computes a client-side
   manifest. Server-built entries (``venv/`` or ``bin/``) are hashed on the
   server after installation and merged in. The release digest
   (``releases/<digest>/``) is the sha256 of the canonical manifest.

4. **No-op Detection**: If the client digest matches the live release's
   digest, nothing changed: transfer/install is skipped entirely (no
   restart); replica counts are still converged. Use ``--force`` to perform
   a full deploy anyway (e.g. to re-run hooks).

5. **Stage and Upload**: The tree is rsynced into
   ``releases/.staging-<pid>/``; unchanged files (mostly dependencies) were
   already server-side deduplicated by hardlinking every byte-identical
   file from the live release, so only changed content is re-uploaded
   (rsync requires key-based SSH auth).

6. **Verify and Commit**: The staging tree is verified on the server against
   ``manifest.json`` (one file entry per path: sha256, mode, kind). Only then
   is it renamed to its immutable release dir ``releases/<digest>/``.
   A verification failure aborts the deploy, deletes the staging tree, and
   leaves the previous release live.

7. **Install Units and Caddyfile**: Systemd unit files, dropins (including
   ``common.d``) and the Caddyfile are installed into the usual system
   locations (``/etc/systemd/system``, ``/etc/caddy/conf.d``).

8. **Activate**: ``current`` is atomically swapped to the new release
   (``current.next`` + ``mv -T``), then services restart according to a
   diff-based restart set (#see "Restart behaviour" below), buildings
   replica counts are converged, and readiness is polled. If services fail
   to start, Fujin automatically offers to roll back to the previously live
   release (auto with ``--no-input``); if you decline, the swap stays in
   place and the deploy exits with an error.

9. **Record and Prune**: The release store (``store/state.json``) is updated,
   old releases are garbage-collected according to ``versions_to_keep``,
   and deployment metadata (version, timestamp, git commit) is appended to
   the audit log.

10. **Completion**: A success message is displayed with deployment details,
   including the application URL if a Caddyfile is configured.

Restart behaviour
~~~~~~~~~~~~~~~~~

The restart set is computed by diffing the previous live release manifest
against the new one:

- **Unit file changed**: restart that unit only (all of its instances).
- **Environment changed**: all app service units are fed from ``.env``, so
  every app service unit restarts.
- **Code changed** (dist sha256 or code entries): restart all app units.
- **Caddyfile changed**: reload Caddy only.
- **Assets changed**: no restart.

Pass ``--full-restart`` to restart every unit instead.

Hooks run in three phases: ``pre_install`` (old release still live),
``post_install`` (new release committed but not yet live; the hook's
``{app_dir}`` resolves to the new release directory), and ``post_start``
(after the ``current`` symlink was swapped and services passed readiness).
A failing ``pre_install`` or ``post_install`` hook fails the deploy (exit
code ``4``). A failing ``post_start`` hook only warns: the release is
already live, so the deploy still succeeds.

Previewing a deploy (``--dry-run``)
------------------------------------

``fujin deploy --dry-run`` prints the plan for the next deploy without
performing it. It is useful for reviewing what a deploy would change on a
production server, or for checking that a configuration change has the
intended effect, before running the real command.

It never mutates anything:

- the build command is **not** executed (no local build, no cache writes);
- no lock is taken and no server layout or app user is created;
- nothing is uploaded, committed, installed or activated;
- no systemd unit is enabled, started, stopped or restarted;
- no hook is run and no ``state.json``/audit-log write happens.

Only read-only SSH queries are made: ``readlink`` for the ``current``
symlink, reads of ``state.json`` and the live ``manifest.json``, and
``systemctl list-unit-files`` / ``systemctl list-units`` for the replica
plan. Secrets are never resolved: environment references are printed as
``<$REF>`` placeholders (never their values). If the preview itself fails
(e.g. the host is unreachable or the configuration is invalid), the normal
exit-code taxonomy applies; a successful preview exits ``0``.

``--dry-run`` performs the same configuration validation as a real deploy,
before it prints anything: unresolved env-template placeholders abort with
the same error/code, an invalid ``[health_check]`` probe is rejected the
same way, and an empty ``.fujin/systemd/`` fails with the same
``No systemd units found, nothing to build`` error. The pre-build
computation (source gathering and the content-addressed ``bundle_key``) is
the same code path the real deploy uses, so the reported cache decision is
the one the build will act on.

The output is plain, one-item-per-line text (no spinners), printed in this
order:

1. **Resolved config**: target host, installation mode, app user and
   replica counts.
2. **Build decision**: the content-addressed ``bundle_key`` and either
   ``cache: cached (<path>)`` when a verified bundle exists under
   ``.fujin/cache/bundles/<key>/`` or ``cache: would run: <build_command>``.
   ``--no-cache`` forces the "would run" answer. On a verified cache hit
   the plan also prints the cached ``client_digest`` and
   ``swap: releases/<digest>`` (the server-built entries and the rendered
   ``.env`` can still change the final digest). Without a cache hit it
   prints ``swap: unknown until build``.
3. **Live release**: the digest the ``current`` symlink points at, the live
   and current config fingerprints, the env reference names, and whether a
   change is expected. ``env: changed|unchanged`` compares the env
   key/ref identity via the config fingerprint (values are never
   compared) — the same identity the live release was built with. The
   expected-change verdict also covers the live fingerprint, a cached
   manifest with a different local version, and (on ``--force``)
   ``change_expected: yes (--force)``. Because the new files are not built
   and secrets are not resolved, the exact new release digest is unknown:
   the plan says so explicitly, and value-only env rotations cannot be
   detected.
4. **Entries by kind**: when a verified cached bundle exists, the plan
   diffs its manifest against the live release and prints added, changed
   and removed paths grouped by kind (``unit``, ``code``, ``asset``,
   ``caddy``). Without a cached bundle it lists the client-known source
   entries and notes that the dist/code entry is unknown until the build
   runs.
5. **Planned transfer**: on a cache hit, the number of client entries that
   would be uploaded; when a live release exists the same line reports how
   many unchanged files are hardlinked from it instead of transferred.
6. **Planned restart set**: the units a real deploy would restart, computed
   with the same diff logic; without a cached bundle it reports
   ``unknown until build``, and ``--full-restart`` reports
   ``all units (--full-restart)``.
7. **Planned convergence**: the replica instances that would be started and
   stopped, based on the live ``systemctl`` state.
8. **Hooks**: the ``pre_install``, ``post_install`` and ``post_start``
   commands that would run.

.. note::

   Server-built entries (``venv/**`` in a release manifest) exist only
   after the server installs the app code. The dry run ignores them on both
   sides of the diff, so a warm cache with an otherwise unchanged release
   reports ``change_expected: no`` and no restart even though the live
   manifest contains venv entries the local cache cannot have.

Example:

.. code-block:: shell

    $ fujin deploy --dry-run
    Dry run: no changes will be made.
    host: prod (vps.example.com)
    installation_mode: python-package
    app_user: bookstore
    replicas: worker=2
    bundle_key: 3f2c...
    cache: cached (.fujin/cache/bundles/3f2c...)
    client_digest: 6d1a...
    swap: releases/6d1a... (server-built entries, and the rendered .env, may change it)
    live_release: 9a1b...
    env_refs: STRIPE_TOKEN=<$STRIPE_TOKEN>
    config_fingerprint: 7d4e... (live: 7d4e...)
    env: unchanged
    change_expected: no
    note: the exact new release digest is unknown until the build runs; secrets are not resolved, so value-only env changes are invisible here.
    entries: no changes
    transfer: 0 files (14 unchanged hardlinked from the live release)
    restart: none
    converge_start: none
    converge_stop: none
    hooks_pre_install: none
    hooks_post_install: uv run manage.py migrate
    hooks_post_start: none

Deployment Layout and Permissions
----------------------------------

All applications are deployed to ``/opt/fujin/{app_name}`` with a secure permission model that separates deployment and runtime privileges.

Directory Structure
~~~~~~~~~~~~~~~~~~~

Every deploy creates an immutable, content-addressed release directory
(``releases/<digest>/``, digest = sha256 of the release manifest), and the
``current`` symlink is the only activation point:

.. code-block:: shell

    /opt/fujin/{app_name}/
    ├── current -> releases/<digest>      # atomic activation symlink
    ├── releases/
    │   ├── <digest>/                     # immutable release tree
    │   │   ├── manifest.json             # file inventory (sha256, mode, kind)
    │   │   ├── .env                      # rendered env file (640)
    │   │   ├── venv/ | bin/              # installed app code
    │   │   ├── systemd/                  # unit files as deployed
    │   │   └── Caddyfile                 # when configured
    │   └── .staging-<pid>/               # transfer target, removed after commit
    ├── store/
    │   ├── lock                          # deploy lock (flock)
    │   └── state.json                    # current/previous digests, deploy log
    ├── shared/                           # persistent app data
    └── db.sqlite3, uploads/...           # app runtime data

``releases/``, ``store/`` and the app dir are setgid ``2755`` owned by
``{deploy_user}:{app_user}``; ``shared/`` is ``2775``. The rendered ``.env``
is mode ``0640`` (group-readable by the app user).

Unit files still live in ``/etc/systemd/system/`` and the Caddyfile in
``/etc/caddy/conf.d/{app_name}.caddy``; unit templates should reference the
release tree through the ``current`` symlink, e.g.
``ExecStart={{ app_dir }}/current/venv/bin/myapp`` or
``EnvironmentFile=-{{ app_dir }}/current/.env``, so the active release
follows the symlink at start time.

Because releases are immutable, application runtime data must live in
``shared/`` (or the app directory root) rather than inside a release
directory.

Shared Python Directory
~~~~~~~~~~~~~~~~~~~~~~~~

Python versions are installed in a shared directory accessible to all applications:

.. code-block:: shell

    /opt/fujin/.python/
    └── cpython-3.13.11-linux-x86_64-gnu/
        └── bin/python3.13

This shared location (configured via ``UV_PYTHON_INSTALL_DIR``) allows
multiple applications to share Python installations, keeps ``ProtectHome=true``
safe, and survives deploy-user changes. The venv inside every release points
at the shared interpreter.

Permission Model
~~~~~~~~~~~~~~~~

Fujin uses a multi-user security model with three components:

1. **fujin group** (``root:fujin``): Members can deploy applications

   - Created during ``fujin server bootstrap``
   - Deploy users are added to this group
   - Grants write access to ``/opt/fujin/``

2. **Deploy user** (e.g., ``tobi``): Owns application files

   - Member of ``fujin`` group
   - Can deploy and manage applications
   - Owns the ``releases/`` and ``store/`` infrastructure (setgid 2755)

3. **App user** (e.g., ``bookstore``): Runs services

   - Non-privileged user created per-application
   - Cannot modify application code or ``current/venv``
   - Can write to database files and logs within app directory
   - Used automatically for ``fujin server exec --appenv`` and ``fujin app`` commands

Immutable release directories and the setgid deployment infrastructure isolate deployment from application runtime data. This means:

- Deployment operations (like ``chown``) only touch ``releases/``, ``store/`` and ``shared/``, not app data
- App runtime files (databases, caches, uploads) remain owned by the app user
- Every deploy atomically swaps ``current``; a failed activation leaves the previous release live

.. note::

   **Running Commands as App User**

   When you use ``fujin server exec --appenv`` or ``fujin app exec``, commands automatically run as the app user (not the deploy user). This ensures commands can write to app-owned files like databases, logs, and uploads.

   For example, Django's ``createsuperuser`` command needs to write to ``db.sqlite3`` (owned by ``bookstore:bookstore``). Running it as the deploy user would fail with permission errors. Fujin handles this automatically:

   .. code-block:: bash

      # These commands run as the app user
      fujin app shell                    # Opens shell as 'bookstore'
      fujin server exec --appenv python  # Runs Python as 'bookstore'
      fujin app exec migrate             # Runs Django migration as 'bookstore'

      # Plain server commands still run as deploy user
      fujin server exec ls -la           # Runs as 'tobi' (deploy user)

Example permissions:

.. code-block:: shell

    /opt/fujin/                      root:fujin       drwxrwxr-x (775)
      ├── .python/                   root:fujin       drwxrwxr-x (775)
      └── bookstore/                 tobi:bookstore   drwxrwxr-x (2755, setgid)
          ├── current                tobi:bookstore   -> releases/<digest>
          ├── releases/              tobi:bookstore   drwxrwsr-x (2755)
          │   └── <digest>/
          │       ├── .env           tobi:bookstore   -rw-r----- (640)
          │       └── venv/          tobi:bookstore   drwxr-xr-x (755)
          ├── store/                 tobi:bookstore   drwxrwsr-x (2755)
          ├── shared/                tobi:bookstore   drwxrwxr-x (2775)
          └── db.sqlite3             bookstore:...    -rw-r--r-- (664)

Security Benefits
~~~~~~~~~~~~~~~~~

This model provides defense-in-depth:

- **Process isolation**: Services run as non-root app user
- **Code protection**: App user cannot modify source code or ``current/venv``
- **Database access**: App user can read/write database files
- **Home directory isolation**: Systemd ``ProtectHome=true`` prevents access to ``/home``
- **System protection**: Systemd ``ProtectSystem=strict`` makes most of filesystem read-only
- **Multi-user support**: Any member of ``fujin`` group can deploy

If an application is compromised, the attacker:

- ✗ Cannot modify application code
- ✗ Cannot access other users' home directories
- ✗ Cannot access other applications' directories (no read/write permissions)
- ✓ Can only read/write within the app directory with group permissions

Migrating Existing Deployments
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Applications deployed with an older fujin (``.install/.venv/`` and
``.install/.versions/*.pyz`` layout) migrate automatically and
**non-destructively**:

1. On the first deploy with the new fujin, you get a one-time notice that
   the legacy layout is detected.
2. A fresh release tree (``current``, ``releases/``, ``store/``, ``shared/``)
   is created and deployed to; the old ``.install/`` and ``.versions/``
   directories are kept in place until you clean them up.
3. ``fujin rollback`` only works after that first release-tree deploy (the
   old zipapp bundles are not used anymore).
4. ``fujin down`` removes the release tree, the systemd units, the app user,
   and any leftover legacy directories.
