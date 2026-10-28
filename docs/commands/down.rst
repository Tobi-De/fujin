down
====

The ``fujin down`` command tears down your application by stopping services and removing resources.

.. image:: ../_static/images/help/down-help.png
   :alt: fujin down command help
   :width: 100%

Overview
--------

Use ``fujin down`` to remove your application from the server. The command stops and disables systemd services, removes systemd unit files, cleans up Caddy configuration, deletes the application user, and removes the application directory.

.. warning::

   This command removes your application from the server. The application directory and all files within it will be deleted. Use with caution.

Options
-------

``-f, --full``
   Also uninstall Caddy web server. Use this if you're completely removing fujin from the server.

``--force``
   Continue teardown even if a teardown step fails. When set, forces removal of the app directory regardless of errors.

How it works
------------

Here's what happens when you run ``fujin down``:

1. **Prompt for confirmation**: Shows a warning message about the irreversible action and asks for confirmation.

2. **Stop services**: Stops and disables all configured systemd services (app services, sockets, timers).

3. **Legacy layout**: If the app still has an old-style ``.versions/*.pyz``
   bundle, Fujin warns that the legacy uninstaller was not run (Fujin does
   not execute interpreters as root) and tells you to run
   ``sudo python3 <bundle> uninstall`` as an administrator. Teardown then
   continues normally.

4. **Remove unit files and app directory**: Deletes the app's systemd unit
   files and wants-symlinks, then the entire application directory
   (``/opt/fujin/your-app``), including the release tree
   (``releases/``, ``store/``, ``shared/``) and any leftover legacy
   ``.install/``/``.versions/`` directories. The application user is
   deleted as well.

5. **Uninstall Caddy** (if ``--full``): Removes the Caddy web server from the system.

6. **Log operation**: Records the teardown to the audit log as either ``down`` or ``full-down``.

If the remove step errors and ``--force`` is not set, the command exits with an error. With ``--force``, it continues and force-removes the app directory using ``rm -rf``.

See Also
--------

- :doc:`deploy` - Deploy application
- :doc:`up` - Bootstrap server
