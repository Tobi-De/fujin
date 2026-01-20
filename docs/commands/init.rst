init
====

The ``fujin init`` command initializes a new project with a ``fujin.toml`` configuration file and ``.fujin/`` directory structure.

.. image:: ../_static/images/help/init-help.png
   :alt: fujin init command help
   :width: 100%

Overview
--------

Use ``fujin init`` to quickly bootstrap your project configuration. It generates:

1. A ``fujin.toml`` file with sensible defaults
2. A ``.fujin/`` directory with systemd unit files and Caddyfile

The command supports different profiles for common frameworks:

- **simple** - Basic Python web application with Gunicorn
- **django** - Django applications with migrations and static files
- **falco** - Falco framework applications with worker process
- **binary** - Self-contained binary deployments

Usage
-----

.. code-block:: bash

   # Initialize with simple profile (default)
   fujin init

   # Initialize with Django profile
   fujin init --profile django

   # Initialize with Falco profile
   fujin init --profile falco

   # Initialize for binary deployment
   fujin init --profile binary

Generated Files
---------------

After running ``fujin init``, you'll have:

.. code-block:: text

   .
   ├── fujin.toml           # Main configuration file
   └── .fujin/
       ├── Caddyfile        # Caddy reverse proxy configuration
       └── systemd/
           └── web.service  # Web server systemd unit

Examples
--------

Simple Profile
~~~~~~~~~~~~~~

.. code-block:: toml
   :caption: fujin.toml

   app = "myapp"
   build_command = "uv build && uv pip compile pyproject.toml -o requirements.txt > /dev/null"
   distfile = "dist/myapp-{version}-py3-none-any.whl"
   requirements = "requirements.txt"
   installation_mode = "python-package"

   [aliases]
   shell = "server exec --appenv bash"
   status = "app info"
   logs = "app logs"
   restart = "app restart"

   [[hosts]]
   user = "deploy"
   address = "myapp.com"
   envfile = ".env.prod"

Django Profile
~~~~~~~~~~~~~~

The Django profile adds ``ExecStartPre`` directives for migrations and static files:

.. code-block:: ini
   :caption: .fujin/systemd/web.service

   [Service]
   # Run migrations and collect static files before starting
   ExecStartPre={app_dir}/.venv/bin/myapp migrate
   ExecStartPre={app_dir}/.venv/bin/myapp collectstatic --no-input
   ExecStartPre=/bin/bash -c 'rsync -a --delete staticfiles/ {app_dir}/staticfiles/'
   ExecStart={app_dir}/.venv/bin/gunicorn myapp.wsgi:application --bind 0.0.0.0:8000

The Caddyfile includes static file handling:

.. code-block:: text
   :caption: .fujin/Caddyfile

   myapp.com {
       handle_path /static/* {
           root * {app_dir}/staticfiles/
           file_server
       }

       handle {
           reverse_proxy localhost:8000
       }
   }

Falco Profile
~~~~~~~~~~~~~

The Falco profile includes a web service with setup command and a worker service:

.. code-block:: text

   .fujin/systemd/
   ├── web.service      # Web server with ExecStartPre for setup
   └── worker.service   # Background worker for db_worker

Binary Profile
~~~~~~~~~~~~~~

The binary profile assumes a self-contained executable:

.. code-block:: toml
   :caption: fujin.toml

   installation_mode = "binary"

.. code-block:: ini
   :caption: .fujin/systemd/web.service

   ExecStartPre={app_dir}/myapp migrate
   ExecStart={app_dir}/myapp prodserver

Next Steps
----------

After running ``fujin init``:

1. Review and customize files in ``.fujin/systemd/``
2. Update ``fujin.toml`` with your host details
3. Create ``.env.prod`` with your environment variables
4. Deploy: ``fujin deploy``
