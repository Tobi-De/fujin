exec
====

Execute arbitrary commands on your server.

.. image:: ../_static/images/help/exec-help.png
   :alt: fujin exec command help
   :width: 100%

Overview
--------

``fujin exec`` provides three ways to run commands on your server:

1. **Plain server command** (default) - Run any command on the server
2. **With app environment** (``--appenv``) - Run in app directory with environment loaded
3. **Via app binary** (``--app``) - Execute through your application binary

Usage
-----

.. code-block:: bash

   fujin exec [OPTIONS] COMMAND [ARGS...]

Options
-------

``-H, --host HOST``
   Target a specific host in multi-host setups.

``--appenv``
   Change to app directory and load environment from ``.appenv`` file.

``--app``
   Execute command via the application binary (e.g., ``myapp migrate``).

.. note::
   The ``--app`` and ``--appenv`` flags are mutually exclusive - you cannot use both together.

Arguments
---------

``COMMAND [ARGS...]``
   The command to execute. Everything after options is passed to the remote command.

Execution Modes
---------------

**Plain Server Command (default)**

Run any command on the server:

.. code-block:: bash

   # Check disk space
   fujin exec df -h

   # View processes
   fujin exec ps aux

   # Any server command
   fujin exec ls -la /var/log

**With App Environment (--appenv)**

Run commands in your app directory with environment variables loaded:

.. code-block:: bash

   # Run Python script with app environment
   fujin exec --appenv python script.py

   # Access database with credentials from .env
   fujin exec --appenv psql -U \$DB_USER -d \$DB_NAME

   # Start interactive bash in app directory
   fujin exec --appenv bash

Equivalent to:

.. code-block:: bash

   cd /path/to/app && source .appenv && your-command

**Via App Binary (--app)**

Execute commands through your application binary:

.. code-block:: bash

   # Django migrations
   fujin exec --app migrate

   # Django shell
   fujin exec --app shell

   # Custom management command
   fujin exec --app my_command

Equivalent to:

.. code-block:: bash

   cd /path/to/app && source .appenv && myapp your-command

Examples
--------

**Django Management Commands**

.. code-block:: bash

   # Run migrations
   fujin exec --app migrate

   # Create superuser
   fujin exec --app createsuperuser

   # Collect static files
   fujin exec --app collectstatic --no-input

   # Open Django shell
   fujin exec --app shell

   # Custom management command
   fujin exec --app my_custom_command

**Database Operations**

.. code-block:: bash

   # Django database shell
   fujin exec --app dbshell

   # Direct PostgreSQL access
   fujin exec --appenv 'psql -U $DB_USER -d $DB_NAME'

   # Export database
   fujin exec pg_dump mydb > backup.sql

**Maintenance and Debugging**

.. code-block:: bash

   # Check app directory contents
   fujin exec --appenv ls -la

   # View environment variables
   fujin exec --appenv env

   # Check Python version in app
   fujin exec --appenv python --version

   # Run health check script
   fujin exec --appenv python healthcheck.py

**Server Commands**

.. code-block:: bash

   # Check disk space
   fujin exec df -h

   # View system logs
   fujin exec tail -f /var/log/syslog

   # Check running processes
   fujin exec ps aux | grep python

   # System info
   fujin exec uname -a

**Interactive Shells**

.. code-block:: bash

   # Django shell
   fujin exec --app shell

   # Bash in app directory
   fujin exec --appenv bash

   # Python REPL with app environment
   fujin exec --appenv python

   # Database shell
   fujin exec --app dbshell

**Multi-Host Operations**

.. code-block:: bash

   # Run on staging
   fujin exec -H staging --app migrate

   # Run on production
   fujin exec -H production --app migrate

   # Check disk on multiple hosts
   fujin exec -H staging df -h
   fujin exec -H production df -h

Common Patterns
---------------

**Using Aliases**

Create shortcuts in ``fujin.toml`` for frequently-used commands:

.. code-block:: toml

   [aliases]
   shell = "exec --app shell"
   migrate = "exec --app migrate"
   bash = "exec --appenv bash"

Then use:

.. code-block:: bash

   fujin shell      # Opens Django shell
   fujin migrate    # Runs migrations
   fujin bash       # Opens bash in app directory

**Running Scripts**

.. code-block:: bash

   # Upload script to server
   scp myscript.py user@server:/path/to/app/

   # Run with app environment
   fujin exec --appenv python myscript.py

**Data Import/Export**

.. code-block:: bash

   # Export Django data
   fujin exec --app dumpdata > data.json

   # Import Django data (after uploading)
   fujin exec --app loaddata data.json

   # Database dump
   fujin exec --appenv 'pg_dump $DB_NAME' > backup.sql


See Also
--------

- :doc:`app` - Application management (safer alternatives)
- :doc:`../configuration` - Configuration reference
- :doc:`deploy` - Deployment workflow

.. tip::

   Create aliases in ``fujin.toml`` for frequently-used commands instead of typing ``fujin exec`` repeatedly.
