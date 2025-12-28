Deploying a Django Application
==============================

This guide walks you through deploying a standard Django application packaged with ``uv``.

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

        [host]
        user = "fujin"
        domain_name = "your-domain.com"
        envfile = ".env.prod"

        [processes]
        # Define the web process. We bind Gunicorn to a Unix socket for better performance.
        web = { command = ".venv/bin/gunicorn bookstore.wsgi:application --bind unix//run/bookstore.sock" }

        [webserver]
        # Tell Caddy to proxy requests to the Gunicorn socket
        upstream = "unix//run/bookstore.sock"
        # Map static files to be served directly by Caddy
        statics = { "/static/*" = "/var/www/bookstore/static/" }

3.  **Release Command**:

    The release command runs every time you deploy. It's the perfect place to run database migrations and collect static files.

    .. code-block:: toml

        release_command = "bookstore migrate && bookstore collectstatic --no-input && sudo rsync --mkpath -a --delete staticfiles/ /var/www/bookstore/static/"

    *   ``bookstore migrate``: Applies database migrations.
    *   ``bookstore collectstatic``: Collects static files to the local ``staticfiles`` folder.
    *   ``rsync``: Syncs the collected files to ``/var/www/...`` where Caddy can serve them. We use ``sudo`` because the ``/var/www`` directory is owned by root/www-data.

Deploy
------

Provision and deploy:

.. code-block:: shell

    fujin up

The ``fujin up`` command is your "first deploy" tool. It performs the following:
1.  **Provisions the server**: Installs necessary system packages (like Python, uv, Caddy).
2.  **Deploys the app**: Uploads your code, installs dependencies, runs the release command, and starts the services.

For subsequent updates (code changes), use ``fujin redeploy`` which is much faster as it skips the provisioning step.

- ``fujin redeploy``: Fast code + env updates
- ``fujin deploy``: Apply config/template changes
