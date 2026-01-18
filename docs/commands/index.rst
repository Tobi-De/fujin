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
   * - :doc:`new`
     - Create new systemd service, timer, or dropin files
   * - :doc:`up`
     - Bootstrap server and deploy application (one-command setup)
   * - :doc:`deploy`
     - Deploy your application to the server
   * - :doc:`app`
     - Manage your application (start, stop, logs, etc.)
   * - :doc:`server`
     - Manage server operations (bootstrap, user creation, status)
   * - :doc:`exec`
     - Execute arbitrary commands on the server
   * - :doc:`scale`
     - Scale services by adjusting replica count
   * - :doc:`rollback`
     - Roll back application to a previous version
   * - :doc:`down`
     - Tear down the project by stopping services and cleaning up
   * - :doc:`prune`
     - Prune old artifacts, keeping only specified number of versions
   * - :doc:`migrate`
     - Migrate from older Fujin configuration formats
   * - :doc:`audit`
     - View audit logs for deployment operations stored on the server

.. toctree::
   :maxdepth: 2
   :hidden:

   init
   new
   up
   deploy
   app
   server
   exec
   scale
   rollback
   down
   prune
   migrate
   audit
