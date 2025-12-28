prune
=====

The ``fujin prune`` command removes old deployment bundles, keeping only a specified number of recent versions.

.. image:: ../_static/images/help/prune-help.png
   :alt: fujin prune command help
   :width: 100%

Overview
--------

Over time, deployment bundles accumulate in ``~/.fujin/{app_name}/.versions/``. The prune command helps manage disk space by removing old versions while keeping recent ones for rollback capability.

Fujin automatically prunes old versions after each deployment based on the ``versions_to_keep`` setting in your ``fujin.toml``. Use ``fujin prune`` to manually clean up versions when you need to free up disk space or keep fewer versions than configured.

Options
-------

``-k, --keep N``
   Number of recent versions to keep (minimum 1). Default: 2.

How it works
------------

Here's what happens when you run ``fujin prune``:

1. **Validate keep count**: Ensures ``--keep`` is at least 1.

2. **Check versions directory**: Verifies the ``.versions/`` directory exists.

3. **List bundles**: Lists all ``.pyz`` files in the versions directory, sorted by modification time (newest first).

4. **Filter valid bundles**: Identifies bundles matching the pattern ``{app_name}-{version}.pyz``.

5. **Determine files to delete**: If there are more bundles than the keep count, selects the oldest bundles for deletion.

6. **Prompt for confirmation**: Shows which versions will be deleted and asks for confirmation before proceeding.

7. **Delete old bundles**: Removes the selected bundle files from the ``.versions/`` directory.

The command is safe - it only removes old deployment bundles and doesn't affect your currently running application or services.

See Also
--------

- :doc:`deploy` - Automatic pruning after deployment
- :doc:`rollback` - Uses kept versions for rollback
