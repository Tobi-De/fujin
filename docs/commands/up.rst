up
==

The ``fujin up`` command bootstraps the server and deploys your application in one step.

.. image:: ../_static/images/help/up-help.png
   :alt: fujin up command help
   :width: 100%

Overview
--------

``fujin up`` is the "first deploy" command. It combines server provisioning and application deployment into a single operation.

Use this command when:

- Setting up a brand new server
- First deployment to a server
- You want to ensure all dependencies are installed

What it does:

1. **Provisions the server** (``fujin server bootstrap``):
   - Installs uv
   - Installs Caddy (if webserver enabled)
   - Sets up system dependencies

2. **Deploys your application** (``fujin deploy``):
   - Builds and uploads your app
   - Installs dependencies
   - Configures services
   - Starts everything

Usage
-----

.. code-block:: bash

   fujin up [OPTIONS]

Options
-------

``-H, --host HOST``
   Target a specific host in multi-host setups. Defaults to the first host.

Examples
--------

**First deployment**

.. code-block:: bash

   fujin up

**Deploy to specific host**

.. code-block:: bash

   fujin up -H production

When to Use
-----------

.. admonition:: Use ``fujin up`` for

   - **First deployment** to a new server
   - **Completely fresh setup** after server rebuild
   - **Testing** - When you want to ensure clean state

.. admonition:: Use ``fujin deploy`` for

   - **Subsequent deployments** (faster, skips bootstrapping)
   - **Code updates**
   - **Configuration changes**

Troubleshooting
---------------

**"uv already installed, skipping"**

This is normal. The bootstrap step detects existing installations and skips them.

**Bootstrap fails**

If server bootstrap fails:

1. Check SSH connection: ``ssh user@server``
2. Verify sudo access: ``ssh user@server sudo ls``
3. Check internet connectivity on server
4. Review error messages for specific issues

**Deploy succeeds but app doesn't work**

Check the application logs:

.. code-block:: bash

   fujin app logs

See Also
--------

- :doc:`deploy` - Deploy command (for updates)
- :doc:`server` - Server management commands
- :doc:`../howtos/index` - Setup guides
