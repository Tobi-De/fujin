Deploy a Django Application
===========================

This guide walks you through deploying a standard Django application packaged with ``uv``.

Prerequisites
-------------

- A Linux server (Ubuntu/Debian) with SSH access
- A domain pointing to the server (or `sslip.io` for testing)
- Fujin installed (see `installation </installation.html>`_)

Server Setup
------------

Fujin provides helper commands to set up your server. These commands handle SSH key setup and user creation.

**Set up SSH access** (if you don't have SSH configured):

.. code-block:: shell

    # Interactive SSH setup wizard
    fujin server setup-ssh

**Create a dedicated deployment user**:

.. code-block:: shell

    # Interactive user creation wizard
    fujin server create-user

These commands ensure your server is properly configured for deployments. For more details, see :doc:`../commands/server`.

Project Setup
-------------

Initialize your Django project with ``uv``:

.. code-block:: shell

    uv tool install django
    django-admin startproject bookstore
    cd bookstore
    uv init --package .
    uv add django gunicorn

The ``uv init --package`` command initializes a packaged application, which is required for Fujin.

By default, ``uv`` creates a ``src`` layout. For Django, it's often easier to use a flat layout and use ``manage.py`` as the entry point.

1.  Remove the default ``src`` directory:

    .. code-block:: shell

        rm -r src

2.  Convert ``manage.py`` to ``__main__.py`` inside the package:

    .. code-block:: shell

        mv manage.py bookstore/__main__.py

    This allows running the app via ``python -m bookstore`` or the installed CLI command.

3.  Update ``pyproject.toml`` to define the script entry point:

    .. code-block:: toml
        :caption: pyproject.toml

        [project.scripts]
        bookstore = "bookstore.__main__:main"

    This exposes a ``bookstore`` command that functions like ``manage.py``.

Initialize Fujin
----------------

.. code-block:: shell

    fujin init

This command does two things:
1.  Creates a ``fujin.toml`` configuration file in your project root.
2.  Creates a ``.fujin`` directory containing template files (e.g., for Systemd services and Caddy configuration). You can customize these templates if needed, but the defaults work for most cases.

Create a production environment file:

.. code-block:: shell

    touch .env.prod

Configuration
-------------

1.  **Django Settings**: Update ``bookstore/settings.py`` to allow your domain and configure static files.

    .. code-block:: python

        ALLOWED_HOSTS = ["your-domain.com"]
        STATIC_ROOT = "./staticfiles"

2.  **Fujin Configuration**: Edit ``fujin.toml``.

    .. code-block:: toml

        [[hosts]]
        user = "fujin"
        address = "your-domain.com"
        envfile = ".env.prod"

        # Define the web process. We bind Gunicorn to a Unix socket for better performance.
        [processes.web]
        command = ".venv/bin/gunicorn bookstore.wsgi:application --bind unix:/run/bookstore.sock"
        listen = "unix//run/bookstore.sock"

        # Configure routing and static files
        [[sites]]
        domains = ["your-domain.com"]
        routes = {
            "/static/*" = { static = "{app_dir}/staticfiles/" },
            "/media/*" = { static = "{app_dir}/media/" },
            "/" = "web"
        }

3.  **Release Command**:

    The release command runs every time you deploy. It's the perfect place to run database migrations and collect static files.

    .. code-block:: toml

        release_command = "bookstore migrate && bookstore collectstatic --no-input"

    *   ``bookstore migrate``: Applies database migrations.
    *   ``bookstore collectstatic``: Collects static files to the ``staticfiles`` folder in your app directory.

    Note: Since we're using ``{app_dir}/staticfiles/`` in the Caddy configuration, static files are served directly from the app directory. No need for rsync to ``/var/www``.

Deploy
------

Provision and deploy:

.. code-block:: shell

    fujin up

The ``fujin up`` command is your "first deploy" tool. It performs the following:
1.  **Provisions the server**: Installs necessary system packages (like Python, uv, Caddy).
2.  **Deploys the app**: Uploads your code, installs dependencies, runs the release command, and starts the services.

For subsequent updates after the initial deployment, use ``fujin deploy``.

Updating Your Application
--------------------------

After the initial deployment with ``fujin up``, use ``fujin deploy`` for all updates:

**Code changes:**

.. code-block:: bash

   # Make your changes
   git commit -am "Fix bug"

   # Deploy to server
   fujin deploy

**Environment variable changes:**

.. code-block:: bash

   # Update .env.prod
   nano .env.prod

   # Redeploy
   fujin deploy

**Configuration changes:**

.. code-block:: bash

   # Update fujin.toml
   nano fujin.toml

   # Deploy with new configuration
   fujin deploy

The ``fujin deploy`` command is fast - it only uploads and installs what changed.
