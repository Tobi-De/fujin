down
====

The ``fujin down`` command tears down your application by stopping services and cleaning up resources.

.. image:: ../_static/images/help/down-help.png
   :alt: fujin down command help
   :width: 100%

Overview
--------

Use ``fujin down`` to remove your application from the server:

- Stops all systemd services
- Disables services from auto-starting
- Removes systemd unit files
- Optionally removes Caddy configuration
- Optionally uninstalls Caddy

.. warning::

   This command removes your application from the server. Your data (database, uploaded files) is NOT automatically deleted, but the application itself is removed.

Usage
-----

.. code-block:: bash

   fujin down [OPTIONS]

Options
-------

``-H, --host HOST``
   Target a specific host in multi-host setups.

``--full``
   Also uninstall Caddy web server. Use this if you're completely removing fujin from the server.

``--force``
   Skip confirmation prompt and force teardown even if errors occur.

Examples
--------

**Standard teardown**

.. code-block:: bash

   fujin down

**Complete removal including Caddy**

.. code-block:: bash

   fujin down --full

**Force removal without prompts**

.. code-block:: bash

   fujin down --force

What Gets Removed
-----------------

**Always removed:**

- Systemd service files from ``/etc/systemd/user/``
- Caddy configuration from ``/etc/caddy/conf.d/``
- Application directory (``~/apps/your-app/`` or custom location)

**NOT removed (your data is safe):**

- Database files
- User uploads
- Log files outside app directory
- System packages (uv, Caddy with ``--full`` not used)

**Removed with --full:**

- Caddy web server

When to Use
-----------

.. admonition:: Temporary removal

   If you're temporarily shutting down the application but plan to redeploy later:

   .. code-block:: bash

      fujin down

.. admonition:: Complete cleanup

   If you're permanently removing the application and won't use fujin on this server anymore:

   .. code-block:: bash

      fujin down --full

.. admonition:: Cleanup before manual removal

   If you want to manually remove files after fujin cleanup:

   .. code-block:: bash

      fujin down
      # Then manually: rm -rf ~/apps/your-app

Common Scenarios
----------------

**Switching to different deployment method**

.. code-block:: bash

   # Clean up fujin deployment
   fujin down --full

   # Then deploy using your new method

**Moving to different server**

.. code-block:: bash

   # On old server
   fujin down

   # On new server
   fujin up

**Debugging deployment issues**

.. code-block:: bash

   # Start fresh
   fujin down
   fujin up

Troubleshooting
---------------

**"Some services failed to stop"**

Services may fail to stop if they're already stopped or don't exist. Use ``--force`` to continue anyway:

.. code-block:: bash

   fujin down --force

**"Permission denied" errors**

Ensure your user has necessary permissions:

.. code-block:: bash

   # Check sudo access
   ssh user@server sudo systemctl status

**Files still remain after down**

The down command removes systemd units and Caddy config, but your application directory remains. To remove everything:

.. code-block:: bash

   fujin down
   ssh user@server
   rm -rf ~/apps/your-app  # or your custom apps_dir

See Also
--------

- :doc:`up` - Bootstrap and deploy
- :doc:`deploy` - Deploy application
- :doc:`rollback` - Roll back to previous version
