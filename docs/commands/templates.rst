templates
=========

The ``fujin templates`` command manages template files for systemd units and Caddy configuration.

.. image:: ../_static/images/help/templates-help.png
   :alt: fujin templates command help
   :width: 100%

Overview
--------

Use ``fujin templates eject`` to copy template files from the fujin package to your local ``.fujin/`` directory. Once ejected, you can customize these templates for your specific needs. Fujin will use local templates from ``.fujin/`` in preference to the package defaults.

Usage
-----

.. code-block:: bash

   # Eject all templates
   fujin templates eject

   # Eject templates for a specific process
   fujin templates eject web
   fujin templates eject worker

   # Eject Caddy template
   fujin templates eject caddy

If a template file already exists in ``.fujin/``, you'll be prompted to confirm before overwriting it.


See Also
--------

- :doc:`../guides/templates` - Template customization guide
- :doc:`deploy` - Deploy application (uses templates)
- :doc:`../configuration` - Configuration reference
