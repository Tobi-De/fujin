show
====

The ``fujin show`` command displays deployment configuration and rendered templates without deploying. It's invaluable for debugging and inspecting your configuration.

.. image:: ../_static/images/help/show-help.png
   :alt: fujin show command help
   :width: 100%

Overview
--------

Use ``fujin show`` to inspect various aspects of your deployment configuration:

- Environment variables (with secret redaction)
- Rendered Caddyfile configuration
- Systemd unit files
- Process configurations

This command is read-only and doesn't make any changes to your server. It's perfect for:

- Debugging configuration issues before deploying
- Verifying template rendering
- Checking environment variable resolution
- Reviewing security settings

Usage
-----

.. code-block:: bash

   fujin show [OPTIONS] [NAME]

Options
-------

``-H, --host HOST``
   Target a specific host in multi-host setups. Defaults to the first host defined in your configuration.

``--plain``
   Show actual secret values instead of redacted placeholders (only for ``env``). Use with caution!

Arguments
---------

``NAME``
   What to show. Can be:

   - ``env`` - Environment variables
   - ``caddy`` - Caddyfile configuration
   - ``units`` - Show all systemd unit configuration files
   - Process name (e.g., ``web``) - Show unit file for that process
   - Unit name (e.g., ``web.service``) - Show specific unit file

Examples
--------

**Show environment variables (with secrets redacted)**

.. code-block:: bash

   fujin show env

**Show environment variables with actual values**

.. code-block:: bash

   fujin show env --plain

⚠️ **Warning:** Be careful with ``--plain`` as it exposes all secrets in plaintext!

**Show rendered Caddyfile**

.. code-block:: bash

   fujin show caddy

**List all systemd units**

.. code-block:: bash

   fujin show units

**Show specific process unit file**

.. code-block:: bash

   fujin show web

**Show timer configuration**

.. code-block:: bash

   fujin show healthcheck.timer

**Show configuration for specific host**

.. code-block:: bash

   fujin show -H staging env


See Also
--------

- :doc:`../configuration` - Configuration reference
- :doc:`deploy` - Deploy command
- :doc:`../secrets` - Secrets management guide
