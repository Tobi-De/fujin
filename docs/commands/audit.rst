audit
=====

View audit logs for deployment operations stored on the server.

.. image:: ../_static/images/help/audit-help.png
   :alt: fujin audit command help
   :width: 100%

Overview
--------

Fujin automatically logs every deployment operation to a server-side audit file. The audit command provides a formatted view of this log, making it easy to:

- Track deployment history across all machines and CI/CD
- Debug deployment issues
- Understand what changed between deployments
- Maintain compliance and accountability

The audit log is stored on the server at ``/opt/fujin/.audit/{app_name}.log``.

Usage
-----

.. code-block:: bash

   fujin audit [OPTIONS]

Options
-------

``-n, --limit N``
   Number of audit entries to show (most recent first). Default: 20.

What Gets Logged
----------------

Fujin logs the following operations:

- **deploy** - Application deployments (includes git commit hash if available)
- **rollback** - Rollback to previous version
- **down** - Teardown operations (stop and remove app)
- **full-down** - Full teardown (also uninstalls proxy)

Each log entry includes:

- ``timestamp``: ISO format timestamp with timezone
- ``operation``: Type of operation (deploy, rollback, down, full-down)
- ``user``: Username who performed the operation
- ``host``: Target host name
- ``app_name``: Application name
- ``version``: Version deployed/rolled back (if applicable)
- ``git_commit``: Git commit hash (for deploy operations)
- ``from_version`` / ``to_version``: For rollback operations

Examples
--------

**View recent deployment history**

.. code-block:: bash

   fujin audit

**View last 5 operations**

.. code-block:: bash

   fujin audit --limit 5

**View more history**

.. code-block:: bash

   fujin audit --limit 50

Audit Log Format
-----------------

**Format:** JSON Lines (JSONL) - one JSON object per line

**Example entry:**

.. code-block:: json

   {"timestamp": "2024-12-28T14:30:00+00:00", "operation": "deploy", "host": "production", "app_name": "myapp", "version": "1.2.3", "git_commit": "abc1234567890", "user": "tobi"}

**File location:** ``/opt/fujin/.audit/{app_name}.log`` on the server

Programmatic Access
-------------------

Since the audit log uses JSONL format, you can process it with standard tools:

.. code-block:: bash

   # SSH to server and analyze logs with jq
   ssh user@server "cat /opt/fujin/.audit/myapp.log" | jq -s 'group_by(.user) | map({user: .[0].user, deployments: length})'

   # Find all rollbacks
   ssh user@server "cat /opt/fujin/.audit/myapp.log" | jq 'select(.operation == "rollback")'

   # Get last deployment with git commit
   ssh user@server "tail -1 /opt/fujin/.audit/myapp.log" | jq '{version, git_commit, timestamp}'

Troubleshooting
---------------

**"No audit logs found"**

You haven't made any deployments yet. Future operations will be logged.

**Slow performance**

Audit logs are fetched via SSH each time. For faster access with large logs:

- Use ``--limit`` to fetch fewer entries
- The command uses ``tail`` efficiently to fetch only recent entries

**Missing deployments from CI/CD**

Ensure your CI/CD pipeline:

- Uses the same remote user
- Successfully completes deployments (audit logs only after successful operations)

**Manual cleanup**

If you want to clean up old audit logs:

.. code-block:: bash

   # SSH to server
   ssh user@server

   # View log size
   wc -l /opt/fujin/.audit/myapp.log

   # Keep only last 100 entries
   tail -100 /opt/fujin/.audit/myapp.log > /opt/fujin/.audit/myapp.log.tmp
   mv /opt/fujin/.audit/myapp.log.tmp /opt/fujin/.audit/myapp.log

   # Or delete entirely
   rm /opt/fujin/.audit/myapp.log

See Also
--------

- :doc:`deploy` - Deploy command
- :doc:`rollback` - Rollback command
- :doc:`down` - Teardown command
