Deployment Guides
=================

Comprehensive, step-by-step guides for deploying applications with fujin.

.. toctree::
   :maxdepth: 1
   :caption: Framework Guides

   django-complete

.. toctree::
   :maxdepth: 1
   :caption: Advanced Guides

   templates

Quick Start Guides
------------------

For quick deployments, see the :doc:`../howtos/index` section.

Guide Comparison
----------------

.. list-table::
   :header-rows: 1
   :widths: 30 35 35

   * - Guide
     - Best For
     - What You'll Learn
   * - :doc:`django-complete`
     - Production Django apps
     - Complete setup with PostgreSQL, Celery, multi-environment, secrets
   * - :doc:`../howtos/django`
     - Quick Django setup
     - Basic deployment to get started fast
   * - :doc:`../howtos/binary`
     - Go/Rust binaries
     - Deploying compiled applications

What's Covered in Complete Guides
----------------------------------

Complete deployment guides include:

✅ **Full production setup** - Database, workers, scheduled tasks
✅ **Multi-environment** - Staging and production configurations
✅ **Secrets management** - Using Bitwarden/1Password
✅ **Static & media files** - Proper file serving with Caddy
✅ **Background processing** - Celery workers and beat scheduler
✅ **Monitoring** - Health checks and logging
✅ **Maintenance** - Backups, rollbacks, troubleshooting
✅ **Best practices** - Security, performance, workflows

Coming Soon
-----------

More comprehensive guides are in development:

- **Flask** - Flask + SQLAlchemy + Celery
- **FastAPI** - FastAPI + async workers
- **Static Sites** - Hugo/Jekyll with automated rebuilds
- **Microservices** - Multi-app deployments

See Also
--------

- :doc:`../configuration` - Configuration reference
- :doc:`../commands/index` - Command documentation
- :doc:`../secrets` - Secrets management
- :doc:`../integrations` - CI/CD integration
