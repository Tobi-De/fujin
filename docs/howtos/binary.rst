Deploying a Binary Application
==============================

This guide covers deploying self-contained executables (Go/Rust/Python via PyApp). Example: PocketBase.

Prerequisites
-------------

- A Linux server (Ubuntu/Debian) with SSH access
- A domain pointing to the server (or `sslip.io` for testing)
- Fujin installed (see `installation </installation.html>`_)

Server Setup
------------

Create a dedicated deployment user:

.. code-block:: shell

    fujin server create-user fujin

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

    [host]
    user = "fujin"
    domain_name = "your-domain.com"

    [processes]
    # Command to run the binary. PocketBase listens on port 8090 by default.
    web = { command = "./pocketbase serve --http 0.0.0.0:8090" }

    [webserver]
    # Tell Caddy to proxy requests to the local port where the binary is listening
    upstream = "localhost:8090"

Deploy
------

.. code-block:: shell

    fujin up

The ``fujin up`` command will:
1.  **Provision**: Install Caddy and other system tools.
2.  **Deploy**: Upload your binary, set up the Systemd service, and configure Caddy to reverse proxy to your app.

Common Operations
-----------------

- ``fujin redeploy``: Fast code + env updates
- ``fujin deploy``: Apply config/template changes
