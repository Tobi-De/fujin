app
===

The ``fujin app`` command provides tools to manage your running application services.

.. image:: ../_static/images/help/app-help.png
   :alt: fujin app command overview
   :width: 100%

.. note::
   The ``fa`` command is available as a convenient shortcut for ``fujin app``.
   For example, ``fa status`` is equivalent to ``fujin app status``.

Overview
--------

Use ``fujin app`` (or the shorter ``fa``) to control your application's systemd services:

- Start, stop, and restart services
- View real-time logs
- Inspect service status
- Execute commands via the application binary
- Scale services to multiple replicas
- Access systemd unit configurations
- View deployment history

The app command works with service names from your ``.fujin/systemd/`` directory and intelligently handles related units (sockets, timers).

Usage Examples
--------------

Given the following systemd units in ``.fujin/systemd/``:

.. code-block:: text

    .fujin/systemd/
    ├── web.service       # Web server with socket activation
    ├── web.socket        # Socket for web service
    ├── worker.service    # Background worker
    └── worker.timer      # Timer for worker (hourly)

You can interact with services in various ways (examples show both ``fujin app`` and the ``fa`` shortcut):

**Manage all services**

.. code-block:: bash

    # Start/Stop/Restart all services (web, worker, socket, timer)
    fa start
    fa stop
    fa restart

**Manage specific process groups**

When targeting a process by name, it includes related units (sockets, timers).

.. code-block:: bash

    # Starts web.service AND web.socket
    fa start web

    # Logs for worker.service AND worker.timer
    fa logs worker

**Manage specific systemd units**

You can be specific by appending the unit type.

.. code-block:: bash

    # Only restart the service, not the socket
    fa restart web.service

    # Only show logs for the timer
    fa logs worker.timer

    # Only stop the socket
    fa stop web.socket

Logs Command
------------

The ``logs`` command is one of the most frequently used app subcommands. It provides various options for viewing application logs.

**Basic usage**

.. code-block:: bash

   # Show logs for all services
   fa logs

   # Show logs for specific process
   fa logs web

   # Show logs for specific unit
   fa logs web.service

**Follow logs in real-time**

.. code-block:: bash

   # Follow logs (like tail -f)
   fa logs -f

   # Follow logs for specific process
   fa logs -f web

**Control log output**

.. code-block:: bash

   # Show last 100 lines (default: 50)
   fujin app logs -n 100

   # Show last 200 lines and follow
   fujin app logs -n 200 -f

   # Show all available logs
   fujin app logs -n 0

**Logs for specific timeframes**

.. code-block:: bash

   # Logs since last hour
   fujin app logs --since "1 hour ago"

   # Logs since specific time
   fujin app logs --since "2024-12-28 14:30:00"

   # Logs from the last day
   fujin app logs --since yesterday

**Log filtering and inspection**

.. code-block:: bash

   # Show only errors (pipe through grep)
   fujin app logs | grep ERROR

   # Follow logs and filter for specific pattern
   fujin app logs -f web | grep "Request processed"

Info Command
------------

Display application information and process status overview.

.. code-block:: bash

   fujin app status

The info command displays:

- Application name and directory
- Local version (from your project)
- Remote version (currently deployed)
- Available rollback targets
- Python version (for python-package mode)
- Running URL (if webserver enabled)
- Status table showing all processes (active/inactive/failed)

Start, Stop, Restart Commands
------------------------------

**Start services**

.. code-block:: bash

   # Start all services
   fujin app start

   # Start specific process (includes socket/timer)
   fujin app start web

   # Start only the service unit
   fujin app start web.service

**Stop services**

.. code-block:: bash

   # Stop all services
   fujin app stop

   # Stop specific process
   fujin app stop worker

   # Stop only the timer
   fujin app stop worker.timer

**Restart services**

.. code-block:: bash

   # Restart all services (useful after config changes)
   fujin app restart

   # Restart specific process
   fujin app restart web

   # Restart to reload environment variables
   fujin app restart

Shell Command
-------------

Open an interactive shell on the server in your app's directory.

.. code-block:: bash

   # Open bash shell in app directory
   fujin app shell

.. important::

   **Runs as app user**: The shell runs as the app user (e.g., ``bookstore``), not the deploy user. This gives you write access to app-owned files like databases, logs, and uploads.

   This is equivalent to: ``fujin server exec --appenv bash``

   **App binary wrapper**: Inside the shell, the app binary command (e.g., ``bookstore``) is automatically wrapped to run as the app user. This means you can simply type:

   .. code-block:: bash

      bookstore@server:/opt/fujin/bookstore$ bookstore createsuperuser
      bookstore@server:/opt/fujin/bookstore$ bookstore migrate

   The wrapper function in ``.appenv`` ensures these commands have the correct permissions without needing ``sudo`` manually.

This is useful for:

- Inspecting deployed files
- Running one-off commands with database write access
- Debugging deployment issues
- Checking file permissions
- Testing commands before creating aliases

Cat Command
-----------

Display systemd unit file contents for your services.

.. code-block:: bash

   # Show unit file for web service
   fujin app cat web

   # Show unit file for specific unit type
   fujin app cat web.service

   # Show all units
   fujin app cat units

   # Show Caddy configuration
   fujin app cat caddy

Exec Command
------------

Execute commands through your application binary as the app user.

**Usage:**

.. code-block:: bash

   fujin app exec COMMAND [ARGS...]
   fa exec COMMAND [ARGS...]  # Using shortcut

This runs commands via your application binary, equivalent to:

.. code-block:: bash

   cd /path/to/app && source .appenv && myapp your-command

**Examples:**

.. code-block:: bash

   # Django migrations
   fujin app exec migrate

   # Django shell
   fujin app exec shell

   # Create superuser
   fujin app exec createsuperuser

   # Custom management command
   fujin app exec my_command

.. tip::

   **Why use app exec:** Commands run as the app user (e.g., ``bookstore``), which has write access to ``db.sqlite3`` and other app-owned files. Running as the deploy user would fail with "read-only database" errors.

**Common patterns with aliases:**

Create shortcuts in ``fujin.toml``:

.. code-block:: toml

   [aliases]
   shell = "app exec shell"
   migrate = "app exec migrate"

Then use:

.. code-block:: bash

   fujin shell      # Opens Django shell
   fujin migrate    # Runs migrations

Scale Command
-------------

Adjust the number of replicas for a service to run multiple instances.

**Usage:**

.. code-block:: bash

   fujin app scale SERVICE COUNT
   fa scale SERVICE COUNT  # Using shortcut

**Scale to multiple replicas:**

.. code-block:: bash

   fujin app scale worker 3

This:

1. Converts ``worker.service`` to ``worker@.service`` (template unit)
2. Updates ``fujin.toml`` with ``[replicas] worker = 3``

After deployment, systemd will run:

- ``myapp-worker@1.service``
- ``myapp-worker@2.service``
- ``myapp-worker@3.service``

**Scale back to single instance:**

.. code-block:: bash

   fujin app scale worker 1

This converts ``worker@.service`` back to ``worker.service`` and removes the replica entry from ``fujin.toml``.

**How it works:**

Fujin uses systemd's template unit feature. Template units have ``@`` in their name and can be instantiated multiple times.

Single instance:

.. code-block:: text

   .fujin/systemd/worker.service
   → deploys as: myapp-worker.service

Multiple replicas:

.. code-block:: text

   .fujin/systemd/worker@.service
   → deploys as: myapp-worker@1.service, myapp-worker@2.service, etc.

The ``%i`` specifier in template units refers to the instance identifier (1, 2, 3, etc.).

.. warning::

   **Socket-activated services:** Scaling socket-activated services is not recommended. Sockets don't scale well because only one socket exists for all replicas. Instead, configure your web server's built-in concurrency:
   
   - Gunicorn: ``--workers N`` or ``--threads N``
   - Uvicorn: ``--workers N``

.. note::

   **Cannot scale to 0:** To stop a service, use ``fujin app stop <service>`` instead. To remove it entirely, delete the service files from ``.fujin/systemd/``.

**Example workflow:**

.. code-block:: bash

   # Create a worker service
   fujin new service worker

   # Scale to 4 workers
   fujin app scale worker 4

   # Deploy
   fujin deploy

   # Check status
   fujin app status

   # View logs for all workers
   fujin app logs worker

   # View logs for specific instance
   fujin app logs myapp-worker@2.service

Common Workflows
----------------

**After deployment, check everything is running**

.. code-block:: bash

   fujin app status
   fujin app logs -n 20

**Debug a failing service**

.. code-block:: bash

   # Check status
   fujin app status web

   # View recent logs
   fujin app logs web -n 100

   # Follow logs while restarting
   fujin app restart web

**Monitor production application**

.. code-block:: bash

   # Follow all logs
   fujin app logs -f

   # Follow only web service logs
   fujin app logs -f web

**After changing environment variables**

.. code-block:: bash

   # Restart to pick up new .env
   fujin app restart

   # Verify the change took effect
   fujin app logs -n 10

**Investigate high memory usage**

.. code-block:: bash

   # Check current status
   fujin app status

   # View logs for memory-related messages
   fujin app logs | grep -i "memory\|oom"

   # Access shell to investigate further
   fujin app shell

**Run database operations interactively**

.. code-block:: bash

   # Open shell as app user (can write to database)
   fujin app shell

   # Now in the shell:
   bookstore@server:/opt/fujin/bookstore$ python manage.py createsuperuser
   bookstore@server:/opt/fujin/bookstore$ python manage.py migrate
   bookstore@server:/opt/fujin/bookstore$ sqlite3 db.sqlite3 ".tables"

.. tip::

   The ``fujin app shell`` command runs as the app user, giving you the same permissions as your running services. This means you can safely modify databases and write to logs without permission errors.

See Also
--------

- :doc:`server` - Server management and ``server exec`` command
- :doc:`deploy` - Deployment workflow and permission model
- :doc:`../configuration` - Configuration options for services and replicas
