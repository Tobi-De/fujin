migrate
=======

The ``fujin migrate`` command converts older Fujin configuration formats to the current file-based structure.

.. image:: ../_static/images/help/migrate-help.png
   :alt: fujin migrate command overview
   :width: 100%

Overview
--------

Older versions of Fujin used a template-based approach where processes and sites were defined in ``fujin.toml``. The current version uses a file-based approach with actual systemd unit files in ``.fujin/systemd/``.

This command automates the migration process.

When to Use
-----------

Run this command if your ``fujin.toml`` contains any of these deprecated sections:

- ``[processes.xxx]`` - Process definitions
- ``[[sites]]`` - Caddy site configuration
- ``[webserver]`` - Webserver settings
- ``release_command`` - Pre-deployment command

Usage
-----

Basic migration
~~~~~~~~~~~~~~~

.. code-block:: bash

   fujin migrate

This will:

1. Generate ``.fujin/systemd/`` directory with service files
2. Generate ``.fujin/Caddyfile`` from sites configuration
3. Update ``fujin.toml`` (remove old sections, add ``[replicas]`` if needed)

Dry run (preview changes)
~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: bash

   fujin migrate --dry-run

Shows what files would be created/modified without making any changes.

Create backup
~~~~~~~~~~~~~

.. code-block:: bash

   fujin migrate --backup

Creates ``fujin.toml.backup`` before making changes.

Example
-------

**Before migration (old format):**

.. code-block:: toml
   :caption: fujin.toml

   app = "myapp"
   build_command = "uv build"
   distfile = "dist/myapp-{version}-py3-none-any.whl"
   installation_mode = "python-package"
   release_command = "myapp migrate"

   [processes.web]
   command = ".venv/bin/gunicorn myapp.wsgi:app --bind localhost:8000"
   listen = "localhost:8000"

   [processes.worker]
   command = ".venv/bin/celery -A myapp worker"
   replicas = 2

   [[sites]]
   domains = ["myapp.com"]
   routes = { "/" = "web" }

   [[hosts]]
   address = "myapp.com"
   user = "deploy"

**After migration (file-based):**

.. code-block:: toml
   :caption: fujin.toml

   app = "myapp"
   build_command = "uv build"
   distfile = "dist/myapp-{version}-py3-none-any.whl"
   installation_mode = "python-package"

   [replicas]
   worker = 2

   [[hosts]]
   address = "myapp.com"
   user = "deploy"

.. code-block:: text

   .fujin/
   ├── Caddyfile
   └── systemd/
       ├── web.service
       └── worker@.service

What Gets Migrated
------------------

**Processes → Service files:**

Each ``[processes.xxx]`` entry becomes a ``.fujin/systemd/xxx.service`` file. If replicas > 1, it becomes ``xxx@.service`` (template unit).

**release_command → ExecStartPre:**

The ``release_command`` is converted to ``ExecStartPre`` directives in the web service file.

**Sites → Caddyfile:**

The ``[[sites]]`` configuration becomes a ``.fujin/Caddyfile`` with:
- Domain configuration
- Route handling (reverse proxy, static files)

**Replicas → [replicas] section:**

Services with ``replicas > 1`` are tracked in a new ``[replicas]`` section.

**Removed:**

- ``apps_dir`` from host configs (now fixed at ``/opt/fujin``)
- ``[webserver]`` section
- ``release_command`` (moved to service file)

Next Steps
----------

After migration:

1. Review generated files in ``.fujin/systemd/``
2. Customize service files if needed (security directives, resource limits)
3. Review ``.fujin/Caddyfile``
4. Test deployment: ``fujin deploy``
