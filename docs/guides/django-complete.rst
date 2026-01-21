Complete Django Deployment Guide
==================================

This comprehensive guide walks you through deploying a production-ready Django application using fujin, showcasing all major features.

.. contents:: On this page
   :local:
   :depth: 2

What You'll Build
-----------------

By the end of this guide, you'll have:

✅ Django app running behind Caddy with automatic SSL
✅ PostgreSQL database with automated migrations
✅ Static and media files properly served
✅ Celery workers for background tasks
✅ Celery Beat for scheduled jobs
✅ Health check monitoring
✅ Secrets managed via Bitwarden
✅ Staging and production environments
✅ Comprehensive logging and monitoring

Prerequisites
-------------

**Local Machine:**

- Python 3.10+ installed
- uv installed (``curl -LsSf https://astral.sh/uv/install.sh | sh``)
- fujin installed (``uv tool install fujin-cli``)
- Bitwarden CLI (optional, for secrets management)

**Server:**

- Ubuntu 20.04+ or Debian-based Linux
- SSH access (root or sudo user)
- Domain name pointing to server IP
- At least 1GB RAM (2GB+ recommended for production)

**Domain Setup:**

- Main domain: ``example.com`` → Production IP
- Subdomain: ``staging.example.com`` → Staging IP (or same server)

1. Project Setup
----------------

Create Django Project
~~~~~~~~~~~~~~~~~~~~~

.. code-block:: bash

   # Create project directory
   mkdir bookstore && cd bookstore

   # Initialize UV project
   uv init --package .

   # Add Django and dependencies
   uv add django gunicorn psycopg2-binary python-dotenv

   # Add Celery for background tasks
   uv add celery redis

   # Create Django project
   uv run django-admin startproject config .

   # Create an app
   uv run python manage.py startapp books

Project Structure
~~~~~~~~~~~~~~~~~

Convert to uv-friendly layout:

.. code-block:: bash

   # Move manage.py to __main__.py for entry point
   mv manage.py config/__main__.py

Update ``pyproject.toml``:

.. code-block:: toml

   [project]
   name = "bookstore"
   version = "0.1.0"
   requires-python = ">=3.10"
   dependencies = [
       "django>=5.0",
       "gunicorn>=21.0",
       "psycopg2-binary>=2.9",
       "python-dotenv>=1.0",
       "celery>=5.3",
       "redis>=5.0",
   ]

   [project.scripts]
   bookstore = "config.__main__:main"

Update ``config/__main__.py``:

.. code-block:: python

   #!/usr/bin/env python
   import os
   import sys

   def main():
       os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
       try:
           from django.core.management import execute_from_command_line
       except ImportError as exc:
           raise ImportError(
               "Couldn't import Django. Are you sure it's installed?"
           ) from exc
       execute_from_command_line(sys.argv)

   if __name__ == '__main__':
       main()

2. Configure Django for Production
-----------------------------------

Update Settings
~~~~~~~~~~~~~~~

Edit ``config/settings.py``:

.. code-block:: python

   import os
   from pathlib import Path
   from dotenv import load_dotenv

   load_dotenv()

   BASE_DIR = Path(__file__).resolve().parent.parent

   # SECURITY
   SECRET_KEY = os.getenv('SECRET_KEY', 'changeme-in-production')
   DEBUG = os.getenv('DEBUG', 'False') == 'True'
   ALLOWED_HOSTS = os.getenv('ALLOWED_HOSTS', 'localhost').split(',')

   # Database
   DATABASES = {
       'default': {
           'ENGINE': 'django.db.backends.postgresql',
           'NAME': os.getenv('DB_NAME', 'bookstore'),
           'USER': os.getenv('DB_USER', 'bookstore'),
           'PASSWORD': os.getenv('DB_PASSWORD', ''),
           'HOST': os.getenv('DB_HOST', 'localhost'),
           'PORT': os.getenv('DB_PORT', '5432'),
       }
   }

   # Static files
   STATIC_URL = '/static/'
   STATIC_ROOT = BASE_DIR / 'staticfiles'

   # Media files
   MEDIA_URL = '/media/'
   MEDIA_ROOT = BASE_DIR / 'mediafiles'

   # Celery Configuration
   CELERY_BROKER_URL = os.getenv('CELERY_BROKER_URL', 'redis://localhost:6379/0')
   CELERY_RESULT_BACKEND = os.getenv('CELERY_RESULT_BACKEND', 'redis://localhost:6379/0')

Create Celery App
~~~~~~~~~~~~~~~~~

Create ``config/celery.py``:

.. code-block:: python

   import os
   from celery import Celery
   from celery.schedules import crontab

   os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

   app = Celery('bookstore')
   app.config_from_object('django.conf:settings', namespace='CELERY')
   app.autodiscover_tasks()

   # Scheduled tasks
   app.conf.beat_schedule = {
       'cleanup-sessions': {
           'task': 'books.tasks.cleanup_sessions',
           'schedule': crontab(hour=2, minute=0),  # 2 AM daily
       },
       'send-daily-report': {
           'task': 'books.tasks.send_daily_report',
           'schedule': crontab(hour=8, minute=0),  # 8 AM daily
       },
   }

Update ``config/__init__.py``:

.. code-block:: python

   from .celery import app as celery_app

   __all__ = ('celery_app',)

Create Management Commands
~~~~~~~~~~~~~~~~~~~~~~~~~~

Create ``books/management/commands/healthcheck.py``:

.. code-block:: python

   from django.core.management.base import BaseCommand
   from django.db import connection

   class Command(BaseCommand):
       help = 'Health check for monitoring'

       def handle(self, *args, **options):
           try:
               # Check database
               connection.ensure_connection()
               self.stdout.write(self.style.SUCCESS('✓ Database OK'))

               # Add more checks here (Redis, etc.)

               self.stdout.write(self.style.SUCCESS('✓ Health check passed'))
               return 0
           except Exception as e:
               self.stdout.write(self.style.ERROR(f'✗ Health check failed: {e}'))
               return 1

3. Initialize Fujin
-------------------

.. code-block:: bash

   # Initialize fujin configuration
   fujin init --profile django

   # Create environment files
   touch .env.staging .env.prod

   # Create .fujin directory for custom templates (optional)
   fujin init --templates

Configure fujin.toml
~~~~~~~~~~~~~~~~~~~~

Edit ``fujin.toml``:

.. code-block:: toml

   app = "bookstore"
   version = "0.1.0"
   python_version = "3.12"

   build_command = "uv build && uv pip compile pyproject.toml -o requirements.txt"
   distfile = "dist/bookstore-{version}-py3-none-any.whl"
   installation_mode = "python-package"
   requirements = "requirements.txt"
   versions_to_keep = 5

   # Run migrations and collect static files on each deploy
   release_command = """
   bookstore migrate --no-input && \
   bookstore collectstatic --no-input && \
   sudo mkdir -p /var/www/bookstore/static /var/www/bookstore/media && \
   sudo rsync -a --delete staticfiles/ /var/www/bookstore/static/ && \
   sudo chown -R $USER:www-data /var/www/bookstore && \
   sudo chmod -R 755 /var/www/bookstore
   """

   # Use Bitwarden for secrets
   [secrets]
   adapter = "bitwarden"
   password_env = "BW_PASSWORD"

   # Staging environment
   [[hosts]]
   name = "staging"
   domain_name = "staging.example.com"
   user = "deploy"
   envfile = ".env.staging"

   # Production environment
   [[hosts]]
   name = "production"
   domain_name = "example.com"
   user = "deploy"
   envfile = ".env.prod"

   # Web server (Gunicorn)
   [processes.web]
   command = ".venv/bin/gunicorn config.wsgi:application --bind unix:/run/bookstore/bookstore.sock --workers 4 --max-requests 1000 --access-logfile - --error-logfile -"
   socket = true

   # Background workers (2 replicas for redundancy)
   [processes.worker]
   command = ".venv/bin/celery -A config worker --loglevel=info --concurrency=4"
   replicas = 2

   # Celery Beat scheduler
   [processes.beat]
   command = ".venv/bin/celery -A config beat --loglevel=info"

   # Health check every 5 minutes
   [processes.healthcheck]
   command = ".venv/bin/bookstore healthcheck"
   timer = { on_calendar = "*:0/5" }

   # Database backup daily at 2 AM
   [processes.backup]
   command = ".venv/bin/bookstore backup_database"
   timer = { on_calendar = "daily 02:00", randomized_delay_sec = "15m" }

   # Caddy reverse proxy
   [webserver]
   upstream = "unix//run/bookstore/bookstore.sock"
   enabled = true

   [webserver.statics]
   "/static/*" = "/var/www/{{ app_name }}/static/"
   "/media/*" = "/var/www/{{ app_name }}/media/"

   # Convenient aliases
   [aliases]
   console = "app exec shell"
   shell = "server exec --appenv bash"
   migrate = "app exec migrate"
   logs-web = "app logs web"
   logs-worker = "app logs worker"

4. Database Setup
-----------------

Install PostgreSQL on Server
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

SSH into your server and install PostgreSQL:

.. code-block:: bash

   ssh deploy@example.com

   # Install PostgreSQL
   sudo apt update
   sudo apt install -y postgresql postgresql-contrib

   # Install Redis (for Celery)
   sudo apt install -y redis-server

Create Database and User
~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: bash

   # Switch to postgres user
   sudo -u postgres psql

In PostgreSQL:

.. code-block:: sql

   -- Create database
   CREATE DATABASE bookstore;

   -- Create user with password
   CREATE USER bookstore WITH PASSWORD 'your-secure-password';

   -- Grant privileges
   GRANT ALL PRIVILEGES ON DATABASE bookstore TO bookstore;

   -- Exit
   \q

Test Connection
~~~~~~~~~~~~~~~

.. code-block:: bash

   psql -h localhost -U bookstore -d bookstore
   # Enter password when prompted
   # If successful: \q to exit

5. Environment Variables & Secrets
-----------------------------------

Store Secrets in Bitwarden
~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: bash

   # Install Bitwarden CLI
   # See: https://bitwarden.com/help/cli/

   # Login
   bw login

   # Store secrets
   bw create item '{"type":2,"name":"bookstore-secret-key","login":{"password":"django-insecure-your-secret-key"}}'
   bw create item '{"type":2,"name":"bookstore-db-password","login":{"password":"your-db-password"}}'

Configure Environment Files
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Edit ``.env.staging``:

.. code-block:: bash

   # Django
   DEBUG=False
   SECRET_KEY=$bookstore-secret-key
   ALLOWED_HOSTS=staging.example.com

   # Database
   DB_NAME=bookstore_staging
   DB_USER=bookstore_staging
   DB_PASSWORD=$bookstore-db-password-staging
   DB_HOST=localhost
   DB_PORT=5432

   # Celery
   CELERY_BROKER_URL=redis://localhost:6379/0
   CELERY_RESULT_BACKEND=redis://localhost:6379/0

Edit ``.env.prod``:

.. code-block:: bash

   # Django
   DEBUG=False
   SECRET_KEY=$bookstore-secret-key-prod
   ALLOWED_HOSTS=example.com

   # Database
   DB_NAME=bookstore
   DB_USER=bookstore
   DB_PASSWORD=$bookstore-db-password
   DB_HOST=localhost
   DB_PORT=5432

   # Celery
   CELERY_BROKER_URL=redis://localhost:6379/1
   CELERY_RESULT_BACKEND=redis://localhost:6379/1

6. Server Provisioning
----------------------

Set Up Deployment User
~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: bash

   # Temporarily configure fujin to use root
   # (Just for user creation)

   # Create deployment user
   fujin server create-user deploy -H staging

   # Update fujin.toml to use 'deploy' user (already done above)

Bootstrap Server
~~~~~~~~~~~~~~~~

.. code-block:: bash

   # Set Bitwarden password
   export BW_PASSWORD="your-bw-password"

   # Bootstrap staging server
   fujin up -H staging

This will:
- Install uv, Caddy, Python
- Deploy your application
- Configure systemd services
- Set up SSL certificates
- Start all services

7. Initial Deployment
---------------------

Deploy to Staging
~~~~~~~~~~~~~~~~~

.. code-block:: bash

   # Deploy to staging
   fujin deploy -H staging

   # Watch logs
   fujin app logs web -H staging

   # Check status
   fujin app status -H staging

Visit ``https://staging.example.com`` - you should see your Django app!

Deploy to Production
~~~~~~~~~~~~~~~~~~~~

After testing on staging:

.. code-block:: bash

   # Deploy to production
   fujin up -H production

   # Verify
   fujin app status -H production

Visit ``https://example.com``

8. Verify Everything Works
---------------------------

Check Services
~~~~~~~~~~~~~~

.. code-block:: bash

   # Check all services
   fujin app status -H production

   # Check specific service logs
   fujin app logs web -H production
   fujin app logs worker -H production
   fujin app logs beat -H production

Test Static Files
~~~~~~~~~~~~~~~~~

.. code-block:: bash

   # Visit https://example.com/static/admin/css/base.css
   # Should load Django admin CSS

Test Health Check
~~~~~~~~~~~~~~~~~

.. code-block:: bash

   # Run health check manually
   fujin app exec healthcheck -H production

   # View healthcheck timer logs
   fujin app logs healthcheck.timer -H production

9. Making Changes
-----------------

Code Changes
~~~~~~~~~~~~

.. code-block:: bash

   # Make changes
   git add .
   git commit -m "Add new feature"

   # Deploy to staging first
   fujin deploy -H staging

   # Test on staging
   # If good, deploy to production
   fujin deploy -H production

Database Migrations
~~~~~~~~~~~~~~~~~~~

.. code-block:: bash

   # Create migration locally
   uv run bookstore makemigrations

   # Commit migration files
   git add books/migrations/
   git commit -m "Add new migration"

   # Deploy (migrations run automatically via release_command)
   fujin deploy -H staging
   fujin deploy -H production

Environment Variable Changes
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: bash

   # Update .env.prod
   nano .env.prod

   # Redeploy to apply changes
   fujin deploy -H production

   # Restart services to pick up new env vars
   fujin app restart -H production

10. Multi-Environment Workflow
-------------------------------

Recommended Workflow
~~~~~~~~~~~~~~~~~~~~

1. **Develop locally**

   .. code-block:: bash

      uv run bookstore runserver

2. **Deploy to staging**

   .. code-block:: bash

      fujin deploy -H staging

3. **Test on staging**

   .. code-block:: bash

      # Run tests
      fujin server exec "pytest" -H staging

      # Check logs
      fujin app logs -H staging

4. **Deploy to production**

   .. code-block:: bash

      fujin deploy -H production

5. **Monitor**

   .. code-block:: bash

      fujin app logs -H production
      fujin audit -H production

11. Monitoring & Maintenance
-----------------------------

View Logs
~~~~~~~~~

.. code-block:: bash

   # All logs
   fujin app logs -H production

   # Specific service
   fujin app logs web -H production

   # Follow logs (like tail -f)
   fujin app logs web -H production -f

   # Last 100 lines
   fujin app logs web -H production -n 100

Check Deployment History
~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: bash

   # View deployment history
   fujin audit -H production

   # View last 10 deployments
   fujin audit --limit 10 -H production

Rollback if Needed
~~~~~~~~~~~~~~~~~~

.. code-block:: bash

   # List available versions
   fujin rollback -H production

   # Roll back to previous version
   fujin rollback -H production

Database Backups
~~~~~~~~~~~~~~~~

Create a backup management command in ``books/management/commands/backup_database.py``:

.. code-block:: python

   from django.core.management.base import BaseCommand
   import subprocess
   from datetime import datetime

   class Command(BaseCommand):
       help = 'Backup database'

       def handle(self, *args, **options):
           timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
           filename = f'/var/backups/bookstore_{timestamp}.sql'

           try:
               subprocess.run([
                   'pg_dump',
                   '-U', 'bookstore',
                   'bookstore',
                   '-f', filename
               ], check=True)

               self.stdout.write(self.style.SUCCESS(f'✓ Backup created: {filename}'))
           except subprocess.CalledProcessError as e:
               self.stdout.write(self.style.ERROR(f'✗ Backup failed: {e}'))

12. Troubleshooting
-------------------

Application Won't Start
~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: bash

   # Check service status
   fujin app status -H production

   # View logs
   fujin app logs web -H production

   # Check environment variables
   fujin show env -H production

   # Test manually
   fujin server exec --appenv ".venv/bin/gunicorn config.wsgi:application" -H production

Static Files Not Loading
~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: bash

   # Check if files exist
   fujin server exec --appenv "ls -la /var/www/bookstore/static/" -H production

   # Check permissions
   fujin server exec --appenv "ls -ld /var/www/bookstore" -H production

   # Re-collect static files
   fujin app exec collectstatic -H production

   # Check Caddy config
   fujin show caddy -H production

Database Connection Issues
~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: bash

   # Test database connection
   fujin server exec --appenv "psql -h localhost -U bookstore -d bookstore -c 'SELECT 1;'" -H production

   # Check database is running
   ssh deploy@example.com sudo systemctl status postgresql

   # Verify credentials
   fujin show env --plain -H production | grep DB_

Workers Not Processing Tasks
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: bash

   # Check worker logs
   fujin app logs worker -H production

   # Check Redis is running
   ssh deploy@example.com sudo systemctl status redis

   # Test Celery connection
   fujin server exec "celery -A config inspect ping" -H production

   # Restart workers
   fujin app restart worker -H production

13. Advanced: Custom Templates
-------------------------------

If you need to customize systemd units or Caddyfile, edit files in ``.fujin/``:

Custom Web Service
~~~~~~~~~~~~~~~~~~

Edit ``.fujin/web.service.j2``:

.. code-block:: jinja

   [Unit]
   Description={{ app_name }} web service
   After=network.target {{ app_name }}.socket

   [Service]
   Type=notify
   User={{ user }}
   Group=www-data
   WorkingDirectory={{ app_dir }}
   EnvironmentFile={{ app_dir }}/.env

   # Custom Gunicorn configuration
   ExecStart={{ app_dir }}/.venv/bin/gunicorn \
       config.wsgi:application \
       --bind unix:/run/{{ app_name }}/{{ app_name }}.sock \
       --workers 4 \
       --max-requests 1000 \
       --timeout 60 \
       --access-logfile - \
       --error-logfile -

   Restart=always
   RestartSec=10

   [Install]
   WantedBy=multi-user.target

Custom Caddyfile
~~~~~~~~~~~~~~~~

Edit ``.fujin/Caddyfile.j2``:

.. code-block:: jinja

   {{ domain_name }} {
       # Custom security headers
       header {
           X-Frame-Options "SAMEORIGIN"
           X-Content-Type-Options "nosniff"
           Referrer-Policy "strict-origin-when-cross-origin"
       }

       # Reverse proxy to Gunicorn
       reverse_proxy {{ upstream }} {
           header_up X-Real-IP {remote_host}
           header_up X-Forwarded-For {remote_host}
       }

       # Static files
       {% for path, directory in statics.items() %}
       handle {{ path }} {
           root * {{ directory }}
           file_server
       }
       {% endfor %}

       # Logging
       log {
           output file /var/log/caddy/{{ app_name }}.log
       }
   }

Deploy Changes
~~~~~~~~~~~~~~

.. code-block:: bash

   # After editing templates
   fujin deploy -H staging

   # Verify rendered output
   fujin show web -H staging
   fujin show caddy -H staging

14. Complete Configuration Reference
-------------------------------------

Here's the complete ``fujin.toml`` with all features:

.. code-block:: toml

   # Application metadata
   app = "bookstore"
   version = "0.1.0"
   python_version = "3.12"

   # Build configuration
   build_command = "uv build && uv pip compile pyproject.toml -o requirements.txt"
   distfile = "dist/bookstore-{version}-py3-none-any.whl"
   installation_mode = "python-package"
   requirements = "requirements.txt"
   versions_to_keep = 5

   # Release command (runs after install, before starting services)
   release_command = """
   bookstore migrate --no-input && \
   bookstore collectstatic --no-input && \
   sudo mkdir -p /var/www/bookstore/static /var/www/bookstore/media && \
   sudo rsync -a --delete staticfiles/ /var/www/bookstore/static/ && \
   sudo chown -R $USER:www-data /var/www/bookstore && \
   sudo chmod -R 755 /var/www/bookstore
   """

   # Secrets management
   [secrets]
   adapter = "bitwarden"
   password_env = "BW_PASSWORD"

   # Staging environment
   [[hosts]]
   name = "staging"
   domain_name = "staging.example.com"
   user = "deploy"
   envfile = ".env.staging"
   ssh_port = 22
   apps_dir = "/opt/apps"

   # Production environment
   [[hosts]]
   name = "production"
   domain_name = "example.com"
   user = "deploy"
   envfile = ".env.prod"
   ssh_port = 22
   apps_dir = "/opt/apps"
   # Process definitions
   [processes.web]
   command = ".venv/bin/gunicorn config.wsgi:application --bind unix:/run/bookstore/bookstore.sock --workers 4 --max-requests 1000 --access-logfile - --error-logfile -"
   socket = true

   [processes.worker]
   command = ".venv/bin/celery -A config worker --loglevel=info --concurrency=4"
   replicas = 2

   [processes.beat]
   command = ".venv/bin/celery -A config beat --loglevel=info"

   [processes.healthcheck]
   command = ".venv/bin/bookstore healthcheck"
   timer = { on_calendar = "*:0/5" }

   [processes.backup]
   command = ".venv/bin/bookstore backup_database"
   timer = { on_calendar = "daily 02:00", randomized_delay_sec = "15m", persistent = true }

   [processes.clearsessions]
   command = ".venv/bin/bookstore clearsessions"
   timer = { on_calendar = "weekly" }

   # Web server configuration
   [webserver]
   upstream = "unix//run/bookstore/bookstore.sock"
   enabled = true
   config_dir = "/etc/caddy/conf.d"

   [webserver.statics]
   "/static/*" = "/var/www/{{ app_name }}/static/"
   "/media/*" = "/var/www/{{ app_name }}/media/"

   # Convenient command aliases
   [aliases]
   console = "app exec shell"
   dbconsole = "app exec dbshell"
   shell = "server exec --appenv bash"
   migrate = "app exec migrate"
   makemigrations = "app exec makemigrations"
   logs-web = "app logs web"
   logs-worker = "app logs worker"
   logs-beat = "app logs beat"

Next Steps
----------

Now that you have a complete Django deployment:

1. **Add monitoring**: Integrate with services like Sentry, DataDog, or Prometheus
2. **Set up CI/CD**: Automate deployments via GitHub Actions or GitLab CI (see :doc:`../integrations`)
3. **Add more features**: Message queues, caching, search (Elasticsearch), etc.
4. **Scale horizontally**: Add more worker replicas or deploy to multiple servers
5. **Backup strategy**: Automate database backups to S3 or similar

See Also
--------

- :doc:`../commands/deploy` - Deployment workflow details
- :doc:`../commands/rollback` - Rolling back failed deployments
- :doc:`../secrets` - Secrets management options
- :doc:`../integrations` - CI/CD integration
- :doc:`../configuration` - Full configuration reference
