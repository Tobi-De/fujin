cache
=====

The ``fujin cache`` command manages the local build cache
(``.fujin/cache/bundles``).

Overview
--------

Build results are content-addressed and cached locally so that repeated
deploys with unchanged inputs can skip the build step entirely. Each cached
bundle holds the assembled release-tree inputs (distribution file, systemd
units, assets, ``requirements.txt``). It never contains the rendered
``.env`` or secret values.

``cache clean``
---------------

Removes all cached build bundles:

.. code-block:: bash

   fujin cache clean

.. code-block:: text

   Removed 3 cached bundle(s).

When something in the build inputs changes, a new cache entry is created and
stale entries are simply left on disk until you clean them. The deploy
command re-verifies cached bundle hashes before reusing them; a corrupted
cache entry forces a full rebuild automatically.

See Also
--------

- :doc:`deploy` - Uses the build cache
