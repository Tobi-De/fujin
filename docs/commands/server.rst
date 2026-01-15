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
- Create deployment users
- Set up SSH keys

Subcommands
-----------

info
~~~~

Display system information about the host.

.. image:: ../_static/images/help/server-info-help.png
   :alt: fujin server info command help
   :width: 80%

Shows OS version, CPU, memory, and other system details using fastfetch when available.

**Example:**

.. code-block:: bash

   $ fujin server info

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

**Check server status**

.. code-block:: bash

   fujin server info

See Also
--------

- :doc:`up` - One-command server setup and deployment
- :doc:`../howtos/index` - Setup guides
- :doc:`../configuration` - Host configuration reference
