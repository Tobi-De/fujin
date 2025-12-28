Template Customization Guide
=============================

Learn how to customize systemd units and Caddyfiles for your specific needs.

.. contents:: On this page
   :local:
   :depth: 2

Overview
--------

Fujin uses Jinja2 templates to generate:

- Systemd service files (``.service``)
- Systemd socket files (``.socket``)
- Systemd timer files (``.timer``)
- Caddyfile configuration

**Default templates** are built into fujin and work for most cases.

**Custom templates** let you override defaults when you need:

- Custom systemd directives
- Additional environment variables
- Custom Caddy configuration
- Host-specific settings

Template Search Path
---------------------

Fujin looks for templates in this order:

1. ``.fujin/<template>.j2`` (your custom templates)
2. Built-in templates (fujin defaults)

**Example:** For a process named ``web``, fujin looks for:

1. ``.fujin/web.service.j2`` (process-specific custom template)
2. ``.fujin/default.service.j2`` (generic custom template)
3. Built-in ``default.service.j2`` (fujin default)

Getting Started
---------------

Initialize Template Directory
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: bash

   # Create .fujin directory with default templates
   fujin init --templates

This copies all built-in templates to ``.fujin/`` where you can modify them.

.. code-block:: text

   .fujin/
   ├── Caddyfile.j2
   ├── default.service.j2
   ├── default.socket.j2
   ├── default.timer.j2
   ├── install.sh.j2
   ├── uninstall.sh.j2
   └── web.service.j2

Template Structure
------------------

Systemd Service Template
~~~~~~~~~~~~~~~~~~~~~~~~~

Basic structure of ``default.service.j2``:

.. code-block:: jinja

   [Unit]
   Description={{ app_name }} {{ process_name }} process
   After=network.target
   {% if process.socket %}
   Requires={{ process_name }}.socket
   After={{ process_name }}.socket
   {% endif %}

   [Service]
   Type={% if process.socket %}notify{% else %}simple{% endif %}
   User={{ user }}
   WorkingDirectory={{ app_dir }}
   EnvironmentFile={{ app_dir }}/.env

   # The command to run
   ExecStart={{ process.command }}

   # Restart policy
   Restart=always
   RestartSec=10

   # Resource limits
   LimitNOFILE=65536

   [Install]
   WantedBy=multi-user.target

Caddyfile Template
~~~~~~~~~~~~~~~~~~

Basic structure of ``Caddyfile.j2``:

.. code-block:: jinja

   {{ domain_name }} {
       # Reverse proxy to application
       reverse_proxy {{ upstream }}

       # Static files
       {% for path, directory in statics.items() %}
       handle {{ path }} {
           root * {{ directory }}
           file_server
       }
       {% endfor %}
   }

Available Template Variables
----------------------------

All templates have access to these variables:

Global Variables
~~~~~~~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Variable
     - Description
   * - ``{{ app_name }}``
     - Application name from fujin.toml
   * - ``{{ version }}``
     - Application version
   * - ``{{ user }}``
     - Deployment user
   * - ``{{ app_dir }}``
     - Full path to application directory
   * - ``{{ domain_name }}``
     - Host domain name
   * - ``{{ upstream }}``
     - Webserver upstream (from ``[webserver]``)
   * - ``{{ statics }}``
     - Static files mapping (dict)

Process Variables
~~~~~~~~~~~~~~~~~

Available in service/socket/timer templates:

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Variable
     - Description
   * - ``{{ process_name }}``
     - Name of the process (e.g., "web", "worker")
   * - ``{{ process.command }}``
     - Command to execute
   * - ``{{ process.replicas }}``
     - Number of replicas
   * - ``{{ process.socket }}``
     - Boolean: socket activation enabled
   * - ``{{ process.timer }}``
     - Timer configuration (if set)

Context Variables
~~~~~~~~~~~~~~~~~

Custom variables from host ``context``:

.. code-block:: toml

   [[hosts]]
   context = {
       workers = "4",
       timeout = "60",
       custom_var = "value"
   }

Access in templates as ``{{ context.workers }}``, etc.

Common Customizations
---------------------

Custom Resource Limits
~~~~~~~~~~~~~~~~~~~~~~~

Edit ``.fujin/web.service.j2``:

.. code-block:: jinja

   [Service]
   # ... existing config ...

   # Memory limits
   MemoryMax=2G
   MemoryHigh=1.5G

   # CPU limits
   CPUQuota=200%  # 2 cores max

   # File descriptor limits
   LimitNOFILE=65536

   # Process limits
   LimitNPROC=512

Custom Environment Variables
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Add environment variables directly in the service file:

.. code-block:: jinja

   [Service]
   EnvironmentFile={{ app_dir }}/.env

   # Additional environment variables
   Environment="PYTHONUNBUFFERED=1"
   Environment="DJANGO_SETTINGS_MODULE=config.settings"
   Environment="LOG_LEVEL={{ context.log_level }}"

Using Host Context
~~~~~~~~~~~~~~~~~~

Make templates adapt to staging vs production:

.. code-block:: toml

   # fujin.toml
   [[hosts]]
   name = "staging"
   context = {
       workers = "2",
       timeout = "30",
       debug_enabled = "true"
   }

   [[hosts]]
   name = "production"
   context = {
       workers = "8",
       timeout = "60",
       debug_enabled = "false"
   }

Template:

.. code-block:: jinja

   [Service]
   ExecStart={{ app_dir }}/.venv/bin/gunicorn \
       --workers {{ context.workers }} \
       --timeout {{ context.timeout }} \
       {% if context.debug_enabled == "true" %}
       --log-level debug \
       {% endif %}
       config.wsgi:application

Custom Restart Policy
~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: jinja

   [Service]
   # Restart behavior
   Restart=on-failure
   RestartSec=5s

   # Give up after 5 attempts
   StartLimitBurst=5
   StartLimitIntervalSec=60s

Pre/Post Start Commands
~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: jinja

   [Service]
   # Run before starting
   ExecStartPre=/bin/mkdir -p /run/{{ app_name }}
   ExecStartPre=/bin/chown {{ user }}:{{ user }} /run/{{ app_name }}

   # Main command
   ExecStart={{ process.command }}

   # Run after stopping
   ExecStopPost=/bin/rm -rf /run/{{ app_name }}

Working Directory and Paths
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: jinja

   [Service]
   WorkingDirectory={{ app_dir }}

   # Add virtualenv to PATH
   Environment="PATH={{ app_dir }}/.venv/bin:/usr/local/bin:/usr/bin:/bin"

   # Python path
   Environment="PYTHONPATH={{ app_dir }}"

Advanced Caddy Configuration
-----------------------------

Custom Headers
~~~~~~~~~~~~~~

Edit ``.fujin/Caddyfile.j2``:

.. code-block:: jinja

   {{ domain_name }} {
       # Security headers
       header {
           # Enable HSTS
           Strict-Transport-Security "max-age=31536000; includeSubDomains"

           # Prevent clickjacking
           X-Frame-Options "SAMEORIGIN"

           # Prevent MIME sniffing
           X-Content-Type-Options "nosniff"

           # XSS protection
           X-XSS-Protection "1; mode=block"

           # Referrer policy
           Referrer-Policy "strict-origin-when-cross-origin"

           # Remove server header
           -Server
       }

       reverse_proxy {{ upstream }}
   }

Rate Limiting
~~~~~~~~~~~~~

.. code-block:: jinja

   {{ domain_name }} {
       # Rate limit: 100 requests per minute
       rate_limit {
           zone static_ip 100r/m
       }

       reverse_proxy {{ upstream }}
   }

Custom Logging
~~~~~~~~~~~~~~

.. code-block:: jinja

   {{ domain_name }} {
       # Custom log format
       log {
           output file /var/log/caddy/{{ app_name }}.log {
               roll_size 100mb
               roll_keep 10
               roll_keep_for 720h
           }

           format json
       }

       reverse_proxy {{ upstream }}
   }

URL Rewriting
~~~~~~~~~~~~~

.. code-block:: jinja

   {{ domain_name }} {
       # Redirect www to non-www
       @www host www.{{ domain_name }}
       redir @www https://{{ domain_name }}{uri} permanent

       # Rewrite API paths
       rewrite /api/* /v1/api{uri}

       reverse_proxy {{ upstream }}
   }

Multiple Upstreams (Load Balancing)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: jinja

   {{ domain_name }} {
       reverse_proxy {{ upstream }} {
           # Health checks
           health_uri /health
           health_interval 10s
           health_timeout 5s

           # Load balancing
           lb_policy random
       }
   }

Process-Specific Templates
---------------------------

Create custom templates for specific processes:

Web Process Template
~~~~~~~~~~~~~~~~~~~~

Create ``.fujin/web.service.j2``:

.. code-block:: jinja

   [Unit]
   Description={{ app_name }} web server
   After=network.target {{ app_name }}.socket

   [Service]
   Type=notify
   User={{ user }}
   Group=www-data
   WorkingDirectory={{ app_dir }}
   EnvironmentFile={{ app_dir }}/.env

   # Gunicorn with custom settings
   ExecStart={{ app_dir }}/.venv/bin/gunicorn \
       --bind unix:/run/{{ app_name }}/{{ app_name }}.sock \
       --workers {{ context.workers | default("4") }} \
       --worker-class sync \
       --max-requests {{ context.max_requests | default("1000") }} \
       --max-requests-jitter 50 \
       --timeout 60 \
       --graceful-timeout 30 \
       --keep-alive 5 \
       --access-logfile - \
       --error-logfile - \
       --log-level {{ context.log_level | default("info") }} \
       config.wsgi:application

   Restart=always
   RestartSec=10

   # Resource limits
   LimitNOFILE=65536
   MemoryMax=2G

   [Install]
   WantedBy=multi-user.target

Worker Process Template
~~~~~~~~~~~~~~~~~~~~~~~~

Create ``.fujin/worker.service.j2``:

.. code-block:: jinja

   [Unit]
   Description={{ app_name }} celery worker %i
   After=network.target redis.service

   [Service]
   Type=simple
   User={{ user }}
   WorkingDirectory={{ app_dir }}
   EnvironmentFile={{ app_dir }}/.env

   # Celery worker
   ExecStart={{ app_dir }}/.venv/bin/celery \
       -A config worker \
       --loglevel={{ context.log_level | default("info") }} \
       --concurrency={{ context.worker_concurrency | default("4") }} \
       --max-tasks-per-child={{ context.max_tasks_per_child | default("1000") }} \
       --time-limit={{ context.task_time_limit | default("3600") }}

   # Graceful shutdown
   KillSignal=SIGTERM
   TimeoutStopSec=300

   Restart=always
   RestartSec=10

   # Resource limits (per worker)
   MemoryMax=1G
   CPUQuota=100%

   [Install]
   WantedBy=multi-user.target

Testing Templates
-----------------

Preview Rendered Templates
~~~~~~~~~~~~~~~~~~~~~~~~~~

Before deploying, check how templates render:

.. code-block:: bash

   # View rendered service file
   fujin show web

   # View rendered Caddyfile
   fujin show caddy

   # View all units
   fujin show units

Local Testing
~~~~~~~~~~~~~

.. code-block:: bash

   # Test Jinja2 syntax locally
   python3 << 'EOF'
   from jinja2 import Template

   template = open('.fujin/web.service.j2').read()
   t = Template(template)

   # Test data
   print(t.render(
       app_name='myapp',
       process_name='web',
       user='deploy',
       app_dir='/home/deploy/apps/myapp',
       process={'command': 'gunicorn app:app'},
       context={'workers': '4'}
   ))
   EOF

Deploy and Verify
~~~~~~~~~~~~~~~~~

.. code-block:: bash

   # Deploy with custom templates
   fujin deploy

   # Check actual systemd unit on server
   ssh user@server systemctl --user cat myapp-web.service

   # Check Caddy config on server
   ssh user@server cat /etc/caddy/conf.d/myapp.caddy

Debugging Templates
-------------------

Common Mistakes
~~~~~~~~~~~~~~~

1. **Jinja2 syntax errors:**

   .. code-block:: jinja

      # Wrong: Missing closing tag
      {% if process.socket %}
      ...
      # Missing: {% endif %}

      # Wrong: Undefined variable
      {{ workers }}  # Use {{ context.workers }}

2. **Systemd syntax errors:**

   .. code-block:: jinja

      # Wrong: Missing quotes in multi-line command
      ExecStart={{ app_dir }}/.venv/bin/gunicorn \
          --workers {{ context.workers }}
          config.wsgi:application  # Missing backslash!

      # Correct:
      ExecStart={{ app_dir }}/.venv/bin/gunicorn \
          --workers {{ context.workers }} \
          config.wsgi:application

3. **Incorrect paths:**

   .. code-block:: jinja

      # Wrong: Relative path
      ExecStart=.venv/bin/gunicorn  # Won't work in systemd

      # Correct: Absolute path
      ExecStart={{ app_dir }}/.venv/bin/gunicorn

Validate Systemd Units
~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: bash

   # After deploying, test the unit
   ssh user@server systemd-analyze verify myapp-web.service

Template Examples
-----------------

Full Django Production Template
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``.fujin/web.service.j2``:

.. code-block:: jinja

   [Unit]
   Description={{ app_name }} web server
   Documentation=https://example.com/docs
   After=network-online.target postgresql.service redis.service
   Wants=network-online.target
   PartOf={{ app_name }}.target

   [Service]
   Type=notify
   User={{ user }}
   Group=www-data
   WorkingDirectory={{ app_dir }}

   # Environment
   EnvironmentFile={{ app_dir }}/.env
   Environment="PYTHONUNBUFFERED=1"
   Environment="DJANGO_SETTINGS_MODULE=config.settings"

   # Security
   PrivateTmp=true
   ProtectSystem=strict
   ProtectHome=true
   ReadWritePaths={{ app_dir }} /run/{{ app_name }}
   NoNewPrivileges=true

   # Resource limits
   LimitNOFILE=65536
   MemoryMax={{ context.memory_limit | default("2G") }}
   CPUQuota={{ context.cpu_quota | default("200%") }}

   # Startup
   ExecStartPre=/bin/mkdir -p /run/{{ app_name }}
   ExecStartPre=/bin/chown {{ user }}:www-data /run/{{ app_name }}

   ExecStart={{ app_dir }}/.venv/bin/gunicorn \
       config.wsgi:application \
       --name {{ app_name }} \
       --bind unix:/run/{{ app_name }}/{{ app_name }}.sock \
       --workers {{ context.workers | default("4") }} \
       --worker-class sync \
       --max-requests {{ context.max_requests | default("1000") }} \
       --max-requests-jitter 50 \
       --timeout 60 \
       --graceful-timeout 30 \
       --access-logfile - \
       --error-logfile - \
       --log-level {{ context.log_level | default("info") }}

   # Shutdown
   KillSignal=SIGTERM
   TimeoutStopSec=30

   # Restart policy
   Restart=on-failure
   RestartSec=5s
   StartLimitBurst=5
   StartLimitIntervalSec=60s

   # Cleanup
   ExecStopPost=/bin/rm -f /run/{{ app_name }}/{{ app_name }}.sock

   [Install]
   WantedBy=multi-user.target

Full Production Caddyfile
~~~~~~~~~~~~~~~~~~~~~~~~~~

``.fujin/Caddyfile.j2``:

.. code-block:: jinja

   {{ domain_name }} {
       # Security headers
       header {
           Strict-Transport-Security "max-age=31536000; includeSubDomains; preload"
           X-Frame-Options "SAMEORIGIN"
           X-Content-Type-Options "nosniff"
           X-XSS-Protection "1; mode=block"
           Referrer-Policy "strict-origin-when-cross-origin"
           Permissions-Policy "geolocation=(), microphone=(), camera=()"
           -Server
       }

       # Logging
       log {
           output file /var/log/caddy/{{ app_name }}.log {
               roll_size 100mb
               roll_keep 10
           }
           format json
       }

       # Rate limiting
       rate_limit {
           zone {{ app_name }} 100r/m
       }

       # Static files (with caching)
       {% for path, directory in statics.items() %}
       handle {{ path }} {
           root * {{ directory }}
           file_server
           header Cache-Control "public, max-age=31536000, immutable"
       }
       {% endfor %}

       # Health check endpoint (no auth)
       handle /health {
           reverse_proxy {{ upstream }}
           header -Server
       }

       # Main application
       reverse_proxy {{ upstream }} {
           # Headers
           header_up X-Real-IP {remote_host}
           header_up X-Forwarded-For {remote_host}
           header_up X-Forwarded-Proto {scheme}

           # Health check
           health_uri /health
           health_interval 10s
           health_timeout 5s

           # Timeouts
           transport http {
               dial_timeout 10s
               response_header_timeout 30s
           }
       }

       # Error pages
       handle_errors {
           respond "{err.status_code} {err.status_text}"
       }
   }

Best Practices
--------------

1. **Start with defaults:** Only customize what you need
2. **Use context variables:** Make templates environment-agnostic
3. **Test locally:** Preview with ``fujin show`` before deploying
4. **Version control:** Commit ``.fujin/`` to git
5. **Document customizations:** Add comments explaining why you changed defaults
6. **Keep it simple:** Complex templates are harder to debug
7. **Use includes:** For shared config, create include files

See Also
--------

- :doc:`../configuration` - Context variables and configuration
- :doc:`../commands/show` - Preview rendered templates
- :doc:`django-complete` - Django deployment with custom templates
- `Jinja2 Documentation <https://jinja.palletsprojects.com/>`_
- `Systemd Service Documentation <https://www.freedesktop.org/software/systemd/man/systemd.service.html>`_
- `Caddyfile Documentation <https://caddyserver.com/docs/caddyfile>`_
