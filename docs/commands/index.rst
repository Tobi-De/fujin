Commands
========

Fujin provides comprehensive commands for managing your deployments.

.. image:: ../_static/images/help/fujin-help.png
   :alt: Fujin command overview
   :width: 100%
   :align: center

Quick Reference
---------------

.. list-table::
   :header-rows: 1
   :widths: 20 80

   * - Command
     - Description
   * - :doc:`init`
     - Initialize a new fujin.toml configuration file
   * - :doc:`up`
     - Bootstrap server and deploy application (one-command setup)
   * - :doc:`deploy`
     - Deploy your application to the server
   * - :doc:`app`
     - Manage your application (start, stop, logs, etc.)
   * - :doc:`server`
     - Manage server operations (bootstrap, user creation, info)
   * - :doc:`show`
     - Show deployment configuration and rendered templates
   * - :doc:`audit`
     - View local audit logs for deployment operations
   * - :doc:`exec`
     - Execute arbitrary commands on the server or via app binary
   * - :doc:`rollback`
     - Roll back application to a previous version
   * - :doc:`down`
     - Tear down the project by stopping services and cleaning up
   * - :doc:`prune`
     - Prune old artifacts, keeping only specified number of versions
   * - :doc:`docs`
     - Open configuration documentation in browser

.. toctree::
   :maxdepth: 2
   :hidden:

   init
   up
   deploy
   app
   server
   show
   audit
   exec
   rollback
   down
   prune
   docs
