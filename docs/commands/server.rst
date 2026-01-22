server
======

The ``fujin server`` command provides server management operations.

.. image:: ../_static/images/help/server-help.png
   :alt: fujin server command help
   :width: 100%

Overview
--------

Use ``fujin server`` to manage server-level operations:

- View server information
- Bootstrap server with required dependencies
- Upgrade server components (Caddy, uv, Python, system packages)
- Create deployment users
- Set up SSH keys
- Execute commands on the server with optional app environment

Subcommands
-----------

status
~~~~~~

Display system information about the host.

.. image:: ../_static/images/help/server-status-help.png
   :alt: fujin server status command help
   :width: 80%

Shows OS version, CPU, memory, and other system details using fastfetch when available.

**Example:**

.. code-block:: bash

   $ fujin server status

bootstrap
~~~~~~~~~

Install system dependencies required for fujin deployments.

.. image:: ../_static/images/help/server-bootstrap-help.png
   :alt: fujin server bootstrap command help
   :width: 80%

This command:

- Creates ``/opt/fujin`` directory structure with proper permissions
- Creates ``/opt/fujin/.python`` shared Python installation directory
- Installs uv (Python package manager)
- Installs Caddy web server (if webserver enabled)
- Sets up necessary system packages
- Configures Caddy to auto-load configurations from ``/etc/caddy/conf.d/``

The bootstrap process creates a shared Python directory at ``/opt/fujin/.python`` where UV installs Python interpreters. This allows:

- Multiple applications to share Python installations
- Stronger systemd security with ``ProtectHome=true`` (home directories completely inaccessible)
- Better isolation between deployment users and runtime users

**Example:**

.. code-block:: bash

   $ fujin server bootstrap

.. note::

   This is automatically run as part of ``fujin up``, so you typically don't need to run it manually.

upgrade
~~~~~~~

Upgrade server components to their latest versions.

This command upgrades:

- **System packages** - Updates all installed packages via apt (non-critical, won't stop other upgrades if it fails)
- **Caddy web server** - Upgrades to the latest version using ``caddy upgrade``
- **uv package manager** - Updates uv to the latest version using ``uv self update``
- **Python installations** - Upgrades all Python versions managed by uv using ``uv python upgrade``

The upgrade command shows version changes (e.g., "Caddy upgraded from v2.7.0 to v2.8.0") and gracefully skips components that aren't installed.

**Example:**

.. code-block:: bash

   $ fujin server upgrade

**Output example:**

.. code-block:: console

   Upgrading server components...
   Upgrading system packages...
   ✓ System packages upgraded successfully!
   Upgrading Caddy web server...
   ✓ Caddy upgraded from v2.7.0 to v2.8.0!
   Upgrading uv package manager...
   ✓ uv is already at the latest version (uv 0.5.0)
   Upgrading Python installations...
   ✓ Python installations upgraded successfully!
   ✓ All server components upgraded successfully!

.. note::

   System package upgrades (apt) are non-critical - if they fail, the command will continue upgrading other components. This ensures Caddy, uv, and Python upgrades can still proceed even if system packages have issues.

create-user
~~~~~~~~~~~

Create a new user with sudo access and SSH key setup.

.. image:: ../_static/images/help/server-create-user-help.png
   :alt: fujin server create-user command help
   :width: 80%

Creates a deployment user with:

- Passwordless sudo access
- SSH keys copied from root user
- Home directory and proper permissions

**Example:**

.. code-block:: bash

   $ fujin server create-user deploy

This creates a user named "deploy" that you can use for deployments.

setup-ssh
~~~~~~~~~

Interactive wizard to set up SSH keys and update fujin.toml.

.. image:: ../_static/images/help/server-setup-ssh-help.png
   :alt: fujin server setup-ssh command help
   :width: 80%

This interactive command:

- Generates SSH keys if needed
- Copies keys to the server
- Updates fujin.toml with key path
- Configures SSH connection settings

**Example:**

.. code-block:: bash

   $ fujin server setup-ssh

Common Workflows
----------------

**Initial server setup**

.. code-block:: bash

   # 1. Set up SSH keys (if not already configured)
   fujin server setup-ssh

   # 2. Create a deployment user
   fujin server create-user deploy

   # 3. Update fujin.toml to use the new user
   # Edit fujin.toml: user = "deploy"

   # 4. Bootstrap the server
   fujin server bootstrap

**Or use the all-in-one command:**

.. code-block:: bash

   fujin up  # Does bootstrap + deploy in one step

**Keep server up to date**

.. code-block:: bash

   # Upgrade all server components
   fujin server upgrade

   # Target specific host
   fujin server upgrade -H production

**Check server status**

.. code-block:: bash

   fujin server status

exec
~~~~

Execute commands on the server, with optional app environment.

**Usage:**

.. code-block:: bash

   fujin server exec [--appenv] COMMAND [ARGS...]

**Options:**

``--appenv``
   Change to app directory and load environment from ``.appenv`` file. Runs as app user.

``-H, --host HOST``
   Target a specific host in multi-host setups.

**Plain Server Command (default):**

Run any command on the server as the deploy user:

.. code-block:: bash

   # Check disk space
   fujin server exec df -h

   # View processes
   fujin server exec ps aux

   # Any server command
   fujin server exec ls -la /var/log

**With App Environment (--appenv):**

Run commands in your app directory with environment variables loaded as the app user:

.. code-block:: bash

   # Run Python script with app environment
   fujin server exec --appenv python script.py

   # Access database with credentials from .env
   fujin server exec --appenv psql -U \$DB_USER -d \$DB_NAME

   # Start interactive bash in app directory
   fujin server exec --appenv bash

This is equivalent to:

.. code-block:: bash

   cd /path/to/app && source .appenv && your-command

.. important::

   **User Context:** Commands run with different permissions depending on the flag:
   
   - **Without --appenv**: Run as the deploy user
   - **With --appenv**: Run as the app user (can write to app-owned files)

**Common patterns with aliases:**

Create shortcuts in ``fujin.toml``:

.. code-block:: toml

   [aliases]
   bash = "server exec --appenv bash"
   logs = "server exec tail -f /var/log/syslog"

**Examples:**

.. code-block:: bash

   # Database operations with environment
   fujin server exec --appenv 'psql -U $DB_USER -d $DB_NAME'

   # Export database
   fujin server exec --appenv 'pg_dump $DB_NAME' > backup.sql

   # Check disk space on production
   fujin server exec df -h -H production

   # Run maintenance script
   fujin server exec --appenv python healthcheck.py

See Also
--------

- :doc:`app` - Application management and ``app exec`` command
- :doc:`up` - One-command server setup and deployment
- :doc:`deploy` - Deployment workflow and permission model
- :doc:`../howtos/index` - Setup guides
- :doc:`../configuration` - Host configuration reference
