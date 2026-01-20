exec
====

Execute arbitrary commands on your server or via your application binary.

.. note::

   The ``exec`` command has been split into two commands:
   
   - ``fujin app exec`` - Execute commands via your application binary
   - ``fujin server exec`` - Execute commands on the server (with optional ``--appenv`` flag)

Overview
--------

``fujin app exec`` and ``fujin server exec`` provide flexible ways to run commands on your server:

1. **Via app binary** (``app exec``) - Execute through your application binary as the app user
2. **Plain server command** (``server exec``) - Run any command on the server as deploy user
3. **With app environment** (``server exec --appenv``) - Run in app directory with environment loaded as app user

fujin app exec
--------------

Execute commands through your application binary.

**Usage:**

.. code-block:: bash

   fujin app exec COMMAND [ARGS...]

**Examples:**

.. code-block:: bash

   # Django migrations
   fujin app exec migrate

   # Django shell
   fujin app exec shell

   # Create superuser
   fujin app exec createsuperuser

   # Custom management command
   fujin app exec my_command

This is equivalent to:

.. code-block:: bash

   cd /path/to/app && source .appenv && myapp your-command

.. tip::

   **Why use app exec:** Commands run as the app user (e.g., ``bookstore``), which has write access to ``db.sqlite3`` and other app-owned files. Running as the deploy user would fail with "read-only database" errors.

fujin server exec
-----------------

Execute commands on the server.

**Usage:**

.. code-block:: bash

   fujin server exec [--appenv] COMMAND [ARGS...]

**Options:**

``--appenv``
   Change to app directory and load environment from ``.appenv`` file. Runs as app user.

``-H, --host HOST``
   Target a specific host in multi-host setups.

**Plain Server Command (default):**

Run any command on the server as the deploy user:

.. code-block:: bash

   # Check disk space
   fujin server exec df -h

   # View processes
   fujin server exec ps aux

   # Any server command
   fujin server exec ls -la /var/log

**With App Environment (--appenv):**

Run commands in your app directory with environment variables loaded:

.. code-block:: bash

   # Run Python script with app environment
   fujin server exec --appenv python script.py

   # Access database with credentials from .env
   fujin server exec --appenv psql -U \$DB_USER -d \$DB_NAME

   # Start interactive bash in app directory
   fujin server exec --appenv bash

Equivalent to:

.. code-block:: bash

   cd /path/to/app && source .appenv && your-command

User Context
------------

.. important::

   Commands run with different user permissions depending on the mode:

   - **Plain server commands** (``server exec``): Run as the deploy user
   - **App environment** (``server exec --appenv``): Run as the app user
   - **Via app binary** (``app exec``): Run as the app user

   This ensures app commands can write to files owned by the app user (databases, logs, uploads, etc.)

Examples
--------

**Django Management Commands**

.. code-block:: bash

   # Run migrations (modifies database)
   fujin app exec migrate

   # Create superuser (writes to database)
   fujin app exec createsuperuser

   # Collect static files
   fujin app exec collectstatic --no-input

   # Open Django shell
   fujin app exec shell

**Database Operations**

.. code-block:: bash

   # Django database shell
   fujin app exec dbshell

   # Direct PostgreSQL access with env vars
   fujin server exec --appenv 'psql -U $DB_USER -d $DB_NAME'

   # Export database (as deploy user)
   fujin server exec pg_dump mydb > backup.sql

**Maintenance and Debugging**

.. code-block:: bash

   # Check app directory contents
   fujin server exec --appenv ls -la

   # View environment variables
   fujin server exec --appenv env

   # Check Python version in app
   fujin server exec --appenv python --version

   # Run health check script
   fujin server exec --appenv python healthcheck.py

**Server Commands**

.. code-block:: bash

   # Check disk space
   fujin server exec df -h

   # View system logs
   fujin server exec tail -f /var/log/syslog

   # Check running processes
   fujin server exec ps aux | grep python

**Interactive Shells**

.. code-block:: bash

   # Django shell
   fujin app exec shell

   # Bash in app directory with environment
   fujin server exec --appenv bash

   # Python REPL with app environment
   fujin server exec --appenv python

**Multi-Host Operations**

.. code-block:: bash

   # Run on staging
   fujin app exec migrate -H staging

   # Run on production
   fujin app exec migrate -H production

   # Check disk on specific host
   fujin server exec df -h -H production

Common Patterns
---------------

**Using Aliases**

Create shortcuts in ``fujin.toml`` for frequently-used commands:

.. code-block:: toml

   [aliases]
   shell = "app exec shell"
   migrate = "app exec migrate"
   bash = "server exec --appenv bash"

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
   fujin server exec --appenv python myscript.py

**Data Import/Export**

.. code-block:: bash

   # Export Django data
   fujin app exec dumpdata > data.json

   # Import Django data (after uploading)
   fujin app exec loaddata data.json

   # Database dump
   fujin server exec --appenv 'pg_dump $DB_NAME' > backup.sql

Troubleshooting
---------------

**Permission Denied Errors**

If you see errors like:

.. code-block:: text

   sqlite3.OperationalError: attempt to write a readonly database
   PermissionError: [Errno 13] Permission denied: 'db.sqlite3'

**Solution**: Use ``app exec`` or ``server exec --appenv`` to run as the app user:

.. code-block:: bash

   # Wrong: Runs as deploy user, can't write to app-owned files
   fujin server exec python manage.py migrate

   # Correct: Runs as app user with write permissions
   fujin app exec migrate
   # Or: fujin server exec --appenv python manage.py migrate

**Sudo Password Required**

If you see ``sudo: a password is required``:

**Solution**: Ensure your deploy user has ``NOPASSWD: ALL`` in sudoers. This is automatically configured by ``fujin server bootstrap``.

**Command Not Found**

If commands like ``python`` or your app binary aren't found:

**Solution**: Use ``server exec --appenv`` to load the app environment which includes ``.venv/bin`` in PATH:

.. code-block:: bash

   # Wrong: PATH doesn't include .venv/bin
   fujin server exec python --version

   # Correct: .appenv loads .venv/bin into PATH
   fujin server exec --appenv python --version

See Also
--------

- :doc:`app` - Application management commands
- :doc:`server` - Server management commands
- :doc:`../configuration` - Configuration reference
- :doc:`deploy` - Deployment workflow and permission model

.. tip::

   Create aliases in ``fujin.toml`` for frequently-used commands instead of typing the full command repeatedly.
