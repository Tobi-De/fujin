scale
=====

The ``fujin app scale`` command adjusts the number of replicas for a service.

Overview
--------

When you need multiple instances of a service (e.g., multiple worker processes), use the ``app scale`` command to convert between single-instance and template-based systemd units.

Usage
-----

Scale to multiple replicas
~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: bash

   fujin app scale worker 3

This:

1. Converts ``worker.service`` to ``worker@.service`` (template unit)
2. Updates ``fujin.toml`` with ``[replicas] worker = 3``

After deployment, systemd will run:
- ``myapp-worker@1.service``
- ``myapp-worker@2.service``
- ``myapp-worker@3.service``

Scale back to single instance
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: bash

   fujin app scale worker 1

This:

1. Converts ``worker@.service`` back to ``worker.service``
2. Removes the replica entry from ``fujin.toml``

How It Works
------------

Fujin uses systemd's template unit feature. Template units have ``@`` in their name and can be instantiated multiple times.

**Single instance:**

.. code-block:: text

   .fujin/systemd/worker.service
   → deploys as: myapp-worker.service

**Multiple replicas:**

.. code-block:: text

   .fujin/systemd/worker@.service
   → deploys as: myapp-worker@1.service, myapp-worker@2.service, etc.

The ``%i`` specifier in template units refers to the instance identifier (1, 2, 3, etc.).

Warnings
--------

**Socket-activated services:**

Scaling socket-activated services is not recommended. Sockets don't scale well because only one socket exists for all replicas. Instead, configure your web server's built-in concurrency:

- Gunicorn: ``--workers N`` or ``--threads N``
- Uvicorn: ``--workers N``

**Cannot scale to 0:**

To stop a service, use ``fujin app stop <service>`` instead. To remove it entirely, delete the service files from ``.fujin/systemd/``.

Example
-------

.. code-block:: bash

   # Create a worker service
   fujin new service worker

   # Edit the service file
   # .fujin/systemd/worker.service

   # Scale to 4 workers
   fujin app scale worker 4

   # Deploy
   fujin deploy

   # Check status
   fujin app info

   # View logs for all workers
   fujin app logs worker

   # View logs for specific instance
   fujin app logs myapp-worker@2.service
