app
===

.. cappa:: fujin.commands.app.App
   :style: terminal
   :terminal-width: 0

Usage Examples
--------------

Given the following configuration in ``fujin.toml``:

.. code-block:: toml

    [processes.web]
    command = "uvicorn app:app"
    socket = true

    [processes.worker]
    command = "celery -A app worker"
    timer = "*:00"  # Run hourly

You can interact with services in various ways:

**Manage all services**

.. code-block:: bash

    # Start/Stop/Restart all services (web, worker, socket, timer)
    fujin app start
    fujin app stop
    fujin app restart

**Manage specific process groups**

When targeting a process by name, it includes related units (sockets, timers).

.. code-block:: bash

    # Starts web.service AND web.socket
    fujin app start web

    # Logs for worker.service AND worker.timer
    fujin app logs worker

**Manage specific systemd units**

You can be specific by appending the unit type.

.. code-block:: bash

    # Only restart the service, not the socket
    fujin app restart web.service

    # Only show logs for the timer
    fujin app logs worker.timer

    # Only stop the socket
    fujin app stop web.socket
