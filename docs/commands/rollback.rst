rollback
========

The ``fujin rollback`` command rolls back your application to a previous version.

.. image:: ../_static/images/help/rollback-help.png
   :alt: fujin rollback command help
   :width: 100%

Overview
--------

When a deployment goes wrong, ``fujin rollback`` quickly reverts to a previous version. The command is fully interactive - it lists all available versions from the ``.versions/`` directory, prompts you to select which version to roll back to, and handles the complete rollback process.

Fujin keeps deployment bundles in ``~/.fujin/{app_name}/.versions/`` as Python zipapps (``.pyz`` files). Each bundle is a self-contained executable containing everything needed to run that version: the application code, dependencies, environment variables, systemd units, and install/uninstall scripts.

How it works
------------

Here's what happens when you run ``fujin rollback``:

1. **List available versions**: Scans the ``.versions/`` directory and lists available versions in reverse chronological order (most recent first).

2. **Prompt for selection**: Asks you to select which version to roll back to, with the most recent version as the default.

3. **Confirm rollback**: Shows the current version and target version, and asks for confirmation before proceeding.

4. **Uninstall current version**: If a bundle exists for the current version, runs its uninstall script to cleanly remove the current deployment.

5. **Install target version**: Extracts and runs the install script from the selected version's bundle.

6. **Clean up newer versions**: Automatically deletes all versions newer than the selected target version to prevent accidentally re-deploying a broken version.

7. **Log operation**: Records the rollback operation to the audit log with from/to version information.

Below is an example of the versions directory structure:

.. code-block:: text

   ~/.fujin/{app_name}/.versions/
   ├── app-1.2.3.pyz
   ├── app-1.2.2.pyz
   ├── app-1.2.1.pyz
   └── app-1.2.0.pyz

.. warning::

   Rollback does NOT automatically revert database migrations. If your deployment included schema changes, you'll need to handle database rollback separately - either restore from backup or manually reverse migrations using your framework's migration tools.

See Also
--------

- :doc:`deploy` - Deploy application
- :doc:`prune` - Manually manage old versions
- :doc:`audit` - View deployment history
