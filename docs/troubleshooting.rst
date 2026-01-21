Troubleshooting
===============

Common issues and solutions when deploying with fujin.

.. contents:: On this page
   :local:
   :depth: 2

Quick Diagnostic Commands
--------------------------

When something goes wrong, start with these:

.. code-block:: bash

   # Check service status
   fujin app status

   # View recent logs
   fujin app logs

   # Check deployment history
   fujin audit

   # Verify configuration
   fujin show env
   fujin show caddy
   fujin show units

   # Check server connectivity
   ssh user@your-server.com

Deployment Issues
-----------------

"Connection refused" or "Permission denied"
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Symptoms:**
- Cannot SSH to server
- fujin commands fail with connection errors

**Solutions:**

1. **Check SSH connection:**

   .. code-block:: bash

      # Test SSH manually
      ssh user@your-server.com

      # Check if key is loaded
      ssh-add -l

      # Add key if needed
      ssh-add ~/.ssh/id_rsa

2. **Verify fujin.toml configuration:**

   .. code-block:: toml

      [[hosts]]
      user = "deploy"  # Correct username?
      domain_name = "example.com"  # Correct domain?
      key_filename = "~/.ssh/id_rsa"  # Correct key path?

3. **Check server firewall:**

   .. code-block:: bash

      ssh user@server sudo ufw status
      # If SSH blocked:
      sudo ufw allow 22/tcp

"Build failed" errors
~~~~~~~~~~~~~~~~~~~~~

**Symptoms:**
- Deployment fails during build phase
- "Command not found" errors

**Solutions:**

1. **Check build_command:**

   .. code-block:: bash

      # Test build locally
      uv build

      # Check pyproject.toml is valid
      cat pyproject.toml

2. **Verify dependencies:**

   .. code-block:: bash

      # Sync dependencies
      uv sync

      # Check if all required packages are listed
      grep dependencies pyproject.toml

3. **Check Python version:**

   .. code-block:: toml

      # In fujin.toml
      python_version = "3.12"  # Matches your local version?

"Upload failed" or checksum mismatch
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Symptoms:**
- Bundle upload succeeds but verification fails
- Checksum errors

**Solutions:**

1. **Retry deployment:**

   .. code-block:: bash

      fujin deploy

2. **Check disk space on server:**

   .. code-block:: bash

      ssh user@server df -h

3. **Network issues - try with smaller timeout:**

   .. code-block:: bash

      # Increase SSH timeout
      ssh user@server 'cat > /dev/null'  # Keep connection alive

Application Won't Start
-----------------------

Services fail to start after deployment
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Symptoms:**
- Deployment succeeds but app doesn't respond
- Services show as "failed" or "inactive"

**Diagnosis:**

.. code-block:: bash

   # Check status
   fujin app status

   # View logs
   fujin app logs web

   # Check systemd status directly
   ssh user@server systemctl --user status yourapp.service

**Common Causes:**

1. **Missing environment variables:**

   .. code-block:: bash

      # Check what's configured
      fujin show env

      # Verify all required vars are set
      fujin show env --plain | grep -E "SECRET_KEY|DATABASE_URL"

2. **Database connection issues:**

   .. code-block:: bash

      # Test database connection
      ssh user@server psql -h localhost -U dbuser -d dbname -c 'SELECT 1;'

   If this fails:
   - Check database is running: ``sudo systemctl status postgresql``
   - Verify credentials in ``.env`` file
   - Check database exists: ``psql -l``

3. **Port/socket already in use:**

   .. code-block:: bash

      # Check what's using the port
      ssh user@server lsof -i :8000

      # Or for socket
      ssh user@server ls -la /run/yourapp/

4. **Permission issues:**

   .. code-block:: bash

      # Check app directory permissions
      ssh user@server ls -la ~/apps/yourapp

      # Fix if needed
      ssh user@server chmod -R 755 ~/apps/yourapp

"ModuleNotFoundError" or import errors
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Symptoms:**
- Python can't find installed packages
- Import errors in logs

**Solutions:**

1. **Check virtual environment:**

   .. code-block:: bash

      # Verify venv exists
      ssh user@server ls ~/apps/yourapp/.venv

      # Check installed packages
      ssh user@server ~/apps/yourapp/.venv/bin/pip list

2. **Reinstall dependencies:**

   .. code-block:: bash

      # SSH to server
      ssh user@server

      # Navigate to app
      cd ~/apps/yourapp

      # Reinstall
      .venv/bin/pip install -r requirements.txt

3. **Check requirements.txt:**

   .. code-block:: bash

      # Verify requirements.txt was included in deployment
      ssh user@server cat ~/apps/yourapp/requirements.txt

"Gunicorn bind failed" errors
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Symptoms:**
- Gunicorn can't bind to socket/port
- "Address already in use" errors

**Solutions:**

1. **Check socket directory exists:**

   .. code-block:: bash

      # Create runtime directory if missing
      ssh user@server mkdir -p /run/yourapp
      ssh user@server chown $USER:$USER /run/yourapp

2. **Kill stale processes:**

   .. code-block:: bash

      # Find processes using the socket
      ssh user@server lsof /run/yourapp/yourapp.sock

      # Kill if needed
      ssh user@server pkill -f gunicorn

      # Restart service
      fujin app restart web

3. **Use systemd socket activation:**

   .. code-block:: toml

      # In fujin.toml
      [processes.web]
      command = ".venv/bin/gunicorn ..."
      socket = true  # Let systemd manage the socket

Web Server Issues
-----------------

"502 Bad Gateway" errors
~~~~~~~~~~~~~~~~~~~~~~~~

**Symptoms:**
- Caddy returns 502
- Site unreachable but Caddy is running

**Diagnosis:**

.. code-block:: bash

   # Check if application is running
   fujin app status

   # Check Caddy logs
   ssh user@server sudo journalctl -u caddy -n 50

   # Check upstream connection
   fujin show caddy  # Verify upstream setting

**Solutions:**

1. **Application not running:**

   .. code-block:: bash

      fujin app start web

2. **Wrong socket path:**

   .. code-block:: bash

      # Check actual socket location
      ssh user@server ls -la /run/yourapp/

      # Update fujin.toml if needed
      [webserver]
      upstream = "unix//run/yourapp/yourapp.sock"  # Match actual path

3. **Permissions on socket:**

   Fujin automatically handles socket permissions by:
   
   - Adding ``UMask=0002`` to service files (makes sockets group-writable)
   - Adding the ``caddy`` user to your app's group during deployment

   If you're experiencing permission issues:

   .. code-block:: bash

      # Verify caddy is in the app group
      ssh user@server groups caddy
      # Should show: caddy : caddy {app_name}

      # If missing, add it manually
      ssh user@server sudo usermod -aG {app_name} caddy
      ssh user@server sudo systemctl restart caddy

      # Check socket permissions
      ssh user@server ls -la /run/{app_name}/
      # Socket should be: srwxrwxr-x (group-writable)

Static files not loading (404 errors)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Symptoms:**
- CSS/JS/images return 404
- Django admin has no styling

**Diagnosis:**

.. code-block:: bash

   # Check if files were collected
   ssh user@server ls -la /var/www/yourapp/static/

   # Check Caddy config
   fujin show caddy

**Solutions:**

1. **Static files not collected:**

   .. code-block:: bash

      # Run collectstatic
      fujin app exec collectstatic --no-input

      # Or redeploy (release_command should collect)
      fujin deploy

2. **Wrong static directory in Caddy:**

   .. code-block:: toml

      # In fujin.toml
      [webserver.statics]
      "/static/*" = "/var/www/{app_name}/static/"  # Check path is correct

3. **Permission issues:**

   .. code-block:: bash

      # Fix permissions
      ssh user@server sudo chown -R $USER:www-data /var/www/yourapp
      ssh user@server sudo chmod -R 755 /var/www/yourapp

SSL certificate issues
~~~~~~~~~~~~~~~~~~~~~~

**Symptoms:**
- HTTPS not working
- "Certificate error" in browser
- Caddy won't start

**Diagnosis:**

.. code-block:: bash

   # Check Caddy status
   ssh user@server sudo systemctl status caddy

   # Check Caddy logs
   ssh user@server sudo journalctl -u caddy -n 100

**Solutions:**

1. **Domain not pointing to server:**

   .. code-block:: bash

      # Check DNS
      dig your-domain.com

      # Should return your server's IP

2. **Port 443 blocked:**

   .. code-block:: bash

      # Check firewall
      ssh user@server sudo ufw status

      # Allow HTTPS if blocked
      ssh user@server sudo ufw allow 443/tcp

3. **Caddy config error:**

   .. code-block:: bash

      # Test Caddy config
      ssh user@server sudo caddy validate --config /etc/caddy/Caddyfile

      # Check generated Caddyfile
      fujin show caddy


Worker/Background Task Issues
------------------------------

Celery workers not processing tasks
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Symptoms:**
- Tasks stuck in pending state
- No worker logs

**Diagnosis:**

.. code-block:: bash

   # Check worker status
   fujin app status | grep worker

   # Check worker logs
   fujin app logs worker

   # Test Celery connection
   fujin server exec "celery -A yourapp inspect ping"

**Solutions:**

1. **Workers not running:**

   .. code-block:: bash

      fujin app start worker

2. **Redis/RabbitMQ not running:**

   .. code-block:: bash

      # Check Redis
      ssh user@server sudo systemctl status redis

      # Start if stopped
      ssh user@server sudo systemctl start redis

3. **Wrong broker URL:**

   .. code-block:: bash

      fujin show env | grep CELERY_BROKER_URL

      # Should match your Redis/RabbitMQ setup

4. **Task not registered:**

   Check that Celery autodiscover is configured:

   .. code-block:: python

      # celery.py
      app.autodiscover_tasks()

Beat scheduler not running tasks
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Symptoms:**
- Scheduled tasks don't run
- Beat logs show no activity

**Solutions:**

1. **Check beat is running:**

   .. code-block:: bash

      fujin app status | grep beat

2. **Check beat schedule:**

   .. code-block:: bash

      fujin app logs beat

3. **Restart beat:**

   .. code-block:: bash

      fujin app restart beat

Timer-based tasks not running
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Symptoms:**
- Systemd timer exists but task doesn't run

**Diagnosis:**

.. code-block:: bash

   # Check timer status
   fujin app logs healthcheck.timer

   # Check when it will run next
   ssh user@server systemctl --user list-timers

**Solutions:**

1. **Timer not enabled:**

   .. code-block:: bash

      ssh user@server systemctl --user enable yourapp-healthcheck.timer
      ssh user@server systemctl --user start yourapp-healthcheck.timer

2. **Check timer configuration:**

   .. code-block:: bash

      fujin show healthcheck.timer

Configuration Issues
--------------------

"Invalid configuration" errors
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Symptoms:**
- fujin commands fail with validation errors
- TOML parsing errors

**Solutions:**

1. **Validate TOML syntax:**

   .. code-block:: bash

      # Use Python to check
      python3 -c "import tomllib; tomllib.load(open('fujin.toml', 'rb'))"

2. **Common TOML mistakes:**

   .. code-block:: toml

      # Wrong: Using quotes for table names
      [host]  # Should be [[hosts]]

      # Wrong: Mixing inline and regular tables
      [processes]
      web = { command = "..." }
      [processes.worker]  # Can't mix inline and block tables

      # Correct:
      [processes.web]
      command = "..."
      [processes.worker]
      command = "..."

3. **Check for required fields:**

   .. code-block:: toml

      # These are required
      app = "myapp"  # App name
      [[hosts]]  # At least one host
      domain_name = "example.com"  # Domain required
      user = "deploy"  # User required

Secrets not resolving
~~~~~~~~~~~~~~~~~~~~~

**Symptoms:**
- Environment variables show as ``$variable`` instead of values
- "Secret not found" errors

**Diagnosis:**

.. code-block:: bash

   # Check if secrets are redacted (good)
   fujin show env

   # Check actual values (careful!)
   fujin show env --plain

**Solutions:**

1. **Secret manager not authenticated:**

   .. code-block:: bash

      # Bitwarden
      bw login
      bw unlock
      export BW_SESSION="..."

      # Or use password env
      export BW_PASSWORD="your-password"

2. **Wrong secret name:**

   .. code-block:: bash

      # List secrets
      bw list items

      # Check exact name matches
      # In .env: SECRET_KEY=$my-secret-key
      # In Bitwarden: Item name must be exactly "my-secret-key"

3. **Secret manager CLI not installed:**

   .. code-block:: bash

      # Install Bitwarden CLI
      # See: https://bitwarden.com/help/cli/


Getting Help
------------

If you're still stuck:

1. **Check logs carefully:**

   .. code-block:: bash

      fujin app logs -n 200  # Last 200 lines

2. **Search GitHub issues:**

   https://github.com/falcopackages/fujin/issues

3. **Create a minimal reproduction:**

   - Fresh Django project
   - Simplest fujin.toml
   - Document exact steps to reproduce

4. **Report the issue:**

   Include:

   - fujin version (``fujin --version``)
   - OS (``uname -a``)
   - fujin.toml (sanitized)
   - Full error output
   - Relevant logs

See Also
--------

- :doc:`configuration` - Configuration reference
- :doc:`commands/audit` - View deployment history
- :doc:`commands/rollback` - Roll back failed deployments
- :doc:`guides/django-complete` - Complete Django setup
