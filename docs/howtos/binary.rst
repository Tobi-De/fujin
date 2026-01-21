Deploy a Binary Application
==============================

This guide covers deploying self-contained executables (Go/Rust/Python via PyApp). Example: PocketBase.

Prerequisites
-------------

- A Linux server (Ubuntu/Debian) with SSH access
- A domain pointing to the server (or `sslip.io` for testing)
- Fujin installed (see `installation </installation.html>`_)

Server Setup
------------

Fujin provides helper commands to set up your server:

.. code-block:: shell

    # Interactive SSH setup wizard (if needed)
    fujin server setup-ssh

    # Interactive user creation wizard
    fujin server create-user

For more details, see :doc:`../commands/server`.

Project Setup
-------------

Download your binary and prepare the folder:

.. code-block:: shell

    mkdir pocketbase && cd pocketbase
    touch .env.prod
    curl -LO https://github.com/pocketbase/pocketbase/releases/download/v0.22.26/pocketbase_0.22.26_linux_amd64.zip
    unzip pocketbase_0.22.26_linux_amd64.zip

Initialize Fujin
----------------

.. code-block:: shell

    fujin init --profile binary

This creates a ``fujin.toml`` configured for binary deployment and a ``.fujin`` directory with templates.

Configuration
-------------

Edit ``fujin.toml`` to match your binary's requirements.

.. code-block:: toml

    app = "pocketbase"
    version = "0.22.26"
    # Command to prepare the binary (e.g., unzip). "true" if nothing to do.
    build_command = "true"
    # Path to the binary file to be uploaded
    distfile = "pocketbase"
    installation_mode = "binary"

    [[hosts]]
    user = "fujin"
    address = "your-domain.com"

    # Command to run the binary. PocketBase listens on port 8090 by default.
    [processes.web]
    command = "./pocketbase serve --http 0.0.0.0:8090"
    listen = "localhost:8090"

    # Tell Caddy to proxy requests to the local port where the binary is listening
    [[sites]]
    domains = ["your-domain.com"]
    routes = { "/" = "web" }

Deploy
------

.. code-block:: shell

    fujin up

The ``fujin up`` command will:
1.  **Provision**: Install Caddy and other system tools.
2.  **Deploy**: Upload your binary, set up the Systemd service, and configure Caddy to reverse proxy to your app.

Common Operations
-----------------

**Update your binary**

.. code-block:: shell

    # Download new version
    curl -LO https://github.com/pocketbase/pocketbase/releases/download/v0.23.0/pocketbase_0.23.0_linux_amd64.zip
    unzip pocketbase_0.23.0_linux_amd64.zip

    # Update version in fujin.toml
    # version = "0.23.0"

    # Deploy the update
    fujin deploy

**View logs**

.. code-block:: shell

    # Follow application logs
    fujin app logs -f

    # Check service status
    fujin app status

**Manage the service**

.. code-block:: shell

    # Restart after config changes
    fujin app restart

    # Stop the application
    fujin app stop

    # Start the application
    fujin app start

For more management commands, see :doc:`../commands/app`.
