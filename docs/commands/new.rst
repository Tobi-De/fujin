new
===

The ``fujin new`` command creates new systemd service, timer, or dropin files in your ``.fujin/systemd/`` directory.

.. image:: ../_static/images/help/new-help.png
   :alt: fujin new command overview
   :width: 100%

Overview
--------

Instead of configuring processes in ``fujin.toml``, Fujin uses a file-based approach where you create actual systemd unit files. The ``new`` command helps generate these files with the correct structure.

Usage
-----

Create a new service
~~~~~~~~~~~~~~~~~~~~

.. code-block:: bash

   fujin new service worker

This creates ``.fujin/systemd/worker.service`` with a basic template:

.. code-block:: ini

   [Unit]
   Description={app_name} worker
   After=network.target

   [Service]
   Type=simple
   User={app_user}
   Group=fujin
   WorkingDirectory={app_dir}
   EnvironmentFile={app_dir}/.env
   ExecStart={app_dir}/.venv/bin/python -m myapp.worker

   [Install]
   WantedBy=multi-user.target

Create a timer
~~~~~~~~~~~~~~

.. code-block:: bash

   fujin new timer cleanup

This creates both files needed for a scheduled task:

- ``.fujin/systemd/cleanup.service`` - The service to run
- ``.fujin/systemd/cleanup.timer`` - The timer configuration

The timer file includes common timer options:

.. code-block:: ini

   [Unit]
   Description={app_name} cleanup timer

   [Timer]
   OnCalendar=daily
   Persistent=true

   [Install]
   WantedBy=timers.target

Create a dropin
~~~~~~~~~~~~~~~

Dropins are configuration snippets that extend or override service settings.

**Common dropin (applies to all services):**

.. code-block:: bash

   fujin new dropin resources

Creates ``.fujin/systemd/common.d/resources.conf``.

**Service-specific dropin:**

.. code-block:: bash

   fujin new dropin --service web memory

Creates ``.fujin/systemd/web.service.d/memory.conf``.

Example dropin content:

.. code-block:: ini

   [Service]
   MemoryMax=512M
   CPUQuota=50%

Variable Substitution
---------------------

All generated files support these variables that are replaced during deployment:

- ``{app_name}`` - Your application name
- ``{app_dir}`` - Full path to application directory (``/opt/fujin/{app_name}``)
- ``{app_user}`` - User to run the app as (defaults to app_name)

Next Steps
----------

After creating a service file:

1. Edit the file to configure your service (command, environment, etc.)
2. Deploy with ``fujin deploy``

For more details on systemd unit files, see the `systemd documentation <https://www.freedesktop.org/software/systemd/man/systemd.service.html>`_.
