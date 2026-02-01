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

2. **Build the Application**: Your application is built locally using the ``build_command`` specified in your configuration.

3. **Create Deployment Bundle**: Fujin creates a Python zipapp (``.pyz`` file) containing:

   - Your distribution file (wheel or binary)
   - Optional ``requirements.txt`` (for Python packages)
   - Resolved ``.env`` file with secrets
   - Rendered systemd unit files (services, sockets, timers)
   - Systemd dropin configurations
   - Caddyfile (if available)
   - Installer script (``_installer/__main__.py``)
   - Installation metadata (``config.json``)

4. **Upload Bundle**: The zipapp is uploaded to a staging file (``.staging.pyz``) in ``{app_dir}/.install/.versions/``, then hardlinked to the versioned filename (``{app_name}-{version}.pyz``).

   - For bundles **≥1MB**: Fujin uses **rsync** if available on both local and remote machines. Rsync performs delta transfers, only uploading changed bytes—significantly faster for subsequent deployments where most content (dependencies) remains unchanged.
   - For bundles **<1MB** or when rsync is unavailable: Fujin uses **SCP** with SHA256 checksum verification.

   The staging file approach ensures that rsync can benefit from previous uploads (even those done via SCP), as it compares against the same ``.staging.pyz`` file.

5. **Execute Installer**: The remote Python interpreter runs the zipapp (``python3 installer.pyz install``), which:

   - Creates the app user if needed
   - Sets up the ``.install/`` directory structure
   - Installs the application (creates virtualenv for Python packages, copies binary for binary mode)
   - Creates ``.appenv`` shell environment setup
   - Installs systemd units and dropin configurations
   - Cleans up stale units and dropins
   - Enables and restarts services
   - Configures and reloads Caddy (when enabled)

6. **Auto-Rollback on Failure**: If services fail to start after installation, Fujin automatically offers to roll back to the previous version. If you confirm (or use ``--no-input``), the previous bundle is re-executed and the failed bundle is removed.

7. **Prune Old Bundles**: Old zipapp bundles are removed from ``.install/.versions/`` according to ``versions_to_keep`` configuration.

8. **Record Deployment**: Deployment metadata (version, timestamp, git commit) is appended to the audit log.

9. **Completion**: A success message is displayed with deployment details, including the application URL if a Caddyfile is configured.

Deployment Layout and Permissions
----------------------------------

All applications are deployed to ``/opt/fujin/{app_name}`` with a secure permission model that separates deployment and runtime privileges.

Directory Structure
~~~~~~~~~~~~~~~~~~~

.. tab-set::

    .. tab-item:: python package

        .. code-block:: shell

            /opt/fujin/{app_name}/
            ├── .install/                         # Deployment infrastructure
            │   ├── .env                          # Environment variables file (640)
            │   ├── .appenv                       # Application-specific environment setup
            │   ├── .version                      # Current deployed version
            │   ├── .venv/                        # Virtual environment (Python from shared dir)
            │   └── .versions/                    # Stored deployment bundles
            │       ├── app-1.2.3.pyz
            │       └── app-1.2.2.pyz
            ├── db.sqlite3                        # App runtime data (owned by app user)
            └── uploads/                          # App runtime data (owned by app user)

    .. tab-item:: binary

        .. code-block:: shell

            /opt/fujin/{app_name}/
            ├── .install/                         # Deployment infrastructure
            │   ├── .env                          # Environment variables file (640)
            │   ├── .appenv                       # Application-specific environment setup
            │   ├── .version                      # Current deployed version
            │   ├── app_binary                    # Installed binary (755)
            │   └── .versions/                    # Stored deployment bundles
            │       ├── app-1.2.3.pyz
            │       └── app-1.2.2.pyz
            ├── data/                             # App runtime data (owned by app user)
            └── cache/                            # App runtime data (owned by app user)

Shared Python Directory
~~~~~~~~~~~~~~~~~~~~~~~~

Python versions are installed in a shared directory accessible to all applications:

.. code-block:: shell

    /opt/fujin/.python/
    └── cpython-3.13.11-linux-x86_64-gnu/
        └── bin/python3.13

This shared location (configured via ``UV_PYTHON_INSTALL_DIR``) allows:

- Multiple applications to share Python installations
- Systemd services to use ``ProtectHome=true`` security directive
- Deploy users to change without breaking existing deployments

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
   - Owns ``.install/`` directory (deployment infrastructure)

3. **App user** (e.g., ``bookstore``): Runs services

   - Non-privileged user created per-application
   - Cannot modify application code or ``.venv``
   - Can write to database files and logs within app directory
   - Used automatically for ``fujin server exec --appenv`` and ``fujin app`` commands

The ``.install/`` subdirectory isolates deployment infrastructure from application runtime data. This means:

- Deployment operations (like ``chown``) only affect ``.install/``, not app data
- App runtime files (databases, caches, uploads) remain owned by the app user
- No risk of permission conflicts between deployment and runtime operations

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

   **Inside the shell**: The ``.appenv`` file contains a wrapper function that automatically runs the app binary (e.g., ``bookstore``) as the app user. This means when you're in ``fujin app shell``, you can simply type ``bookstore migrate`` and it will work correctly without manual ``sudo``.

Example permissions:

.. code-block:: shell

    /opt/fujin/                      root:fujin       drwxrwxr-x (775)
      ├── .python/                   root:fujin       drwxrwxr-x (775)
      └── bookstore/                 tobi:bookstore   drwxrwxr-x (775)
          ├── .install/              tobi:bookstore   drwxrwx--- (770)
          │   ├── .env               tobi:bookstore   -rw-r----- (640)
          │   └── .venv/             tobi:bookstore   drwxr-xr-x (755)
          └── db.sqlite3             bookstore:...    -rw-r--r-- (664)

Security Benefits
~~~~~~~~~~~~~~~~~

This model provides defense-in-depth:

- **Process isolation**: Services run as non-root app user
- **Code protection**: App user cannot modify source code or ``.venv``
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
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

If you have applications deployed before the shared Python directory feature was added, you need to migrate them to use the new security model.

**Symptoms of unmigrated deployments:**

- Services fail with exit code 203/EXEC
- Python installed in ``~/.local/share/uv/python``
- Systemd units using ``ProtectHome=read-only`` (old insecure setting)

**Migration steps:**

1. **Update systemd service files** in ``.fujin/systemd/*.service``:

   .. code-block:: ini

      # Change from:
      ProtectHome=read-only
      ReadWritePaths={app_dir}/.venv

      # To:
      ProtectHome=true
      ReadWritePaths={app_dir}

2. **Run bootstrap** to create shared Python directory:

   .. code-block:: bash

      fujin server bootstrap

   This creates ``/opt/fujin/.python`` with proper permissions.

3. **Redeploy your application**:

   .. code-block:: bash

      fujin deploy

   The installer will automatically:

   - Install Python to ``/opt/fujin/.python``
   - Recreate the venv pointing to the shared Python
   - Apply the updated systemd units with ``ProtectHome=true``

**Verification:**

After redeployment, verify the migration:

.. code-block:: bash

   # Check Python location
   ssh user@host "readlink /opt/fujin/myapp/.venv/bin/python"
   # Should show: /opt/fujin/.python/cpython-3.x.x-.../bin/python3.x

   # Verify systemd security
   ssh user@host "systemd-analyze security myapp-web.service | grep ProtectHome"
   # Should show: ✓ ProtectHome=true

   # Check services are running
   fujin app status

**Notes:**

- Old Python installations in ``~/.local/share/uv/python`` are not automatically removed
- They can be manually deleted to free disk space
- Multiple applications share the same Python installation in ``/opt/fujin/.python``
- If you have multiple apps, redeploy each one to migrate them all
