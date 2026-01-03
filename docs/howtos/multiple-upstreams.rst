Multiple Upstreams with Path-Based Routing
===========================================

Sometimes you need to route different URL paths to different backend servers. A common use case is running both a WSGI server (like Gunicorn) for regular HTTP requests and an ASGI server (like Daphne) for WebSocket connections in a Django application.

Overview
--------

Fujin's webserver configuration supports two modes:

1. **Simple mode**: Single ``upstream`` for all traffic
2. **Advanced mode**: Multiple ``routes`` with path-based routing

You cannot use both modes simultaneously - choose one based on your needs.

Simple Mode (Single Upstream)
------------------------------

Use this when all requests go to the same backend:

.. code-block:: toml
    :caption: fujin.toml

    [webserver]
    upstream = "unix//run/app/gunicorn.sock"
    statics = { "/static/*" = "/var/www/app/static/" }

Advanced Mode (Multiple Upstreams)
-----------------------------------

Use ``routes`` when you need to route different paths to different backends:

.. code-block:: toml
    :caption: fujin.toml

    [webserver]
    statics = { "/static/*" = "/var/www/app/static/" }

    [[webserver.routes]]
    path = "/ws"
    upstream = "localhost:8001"

    [[webserver.routes]]
    path = "/api"
    upstream = "localhost:8002"

    [[webserver.routes]]
    path = "/"
    upstream = "unix//run/app/gunicorn.sock"

Route Configuration
~~~~~~~~~~~~~~~~~~~

Each route accepts:

- **path** (required): URL path to match (must start with ``/``)
- **upstream** (required): Backend server address
- **strip_path** (optional): Remove the matched path before proxying (default: false)

Routes are evaluated in order, and the first matching route handles the request.

Example: Django with WSGI and ASGI
-----------------------------------

Here's a complete example of a Django application that runs:

- Gunicorn (WSGI) for regular HTTP requests
- Daphne (ASGI) for WebSocket connections

.. code-block:: toml
    :caption: fujin.toml

    app = "myapp"
    requirements = "requirements.txt"
    python_version = "3.13"
    build_command = "uv build"
    release_command = "myapp migrate && myapp collectstatic --no-input"
    distfile = "dist/myapp-{version}-py3-none-any.whl"
    installation_mode = "python-package"

    [webserver]
    statics = { "/static/*" = "/var/www/myapp/static/" }

    # WebSocket connections go to Daphne (ASGI)
    [[webserver.routes]]
    path = "/ws"
    upstream = "localhost:8001"

    # Everything else goes to Gunicorn (WSGI)
    [[webserver.routes]]
    path = "/"
    upstream = "unix//run/myapp/gunicorn.sock"

    [processes]
    # WSGI server for regular HTTP
    web = { command = ".venv/bin/gunicorn myapp.wsgi:application --bind unix:/run/myapp/gunicorn.sock" }
    
    # ASGI server for WebSockets
    asgi = { command = ".venv/bin/daphne -b 127.0.0.1 -p 8001 myapp.asgi:application" }
    
    # Background workers
    worker = { command = ".venv/bin/myapp worker", replicas = 2 }

    [[hosts]]
    domain_name = "example.com"
    user = "ubuntu"
    envfile = ".env.prod"

Example: Microservices Architecture
------------------------------------

Route different API endpoints to different services:

.. code-block:: toml
    :caption: fujin.toml

    [webserver]
    # User service
    [[webserver.routes]]
    path = "/api/users"
    upstream = "localhost:8001"

    # Order service
    [[webserver.routes]]
    path = "/api/orders"
    upstream = "localhost:8002"

    # Payment service
    [[webserver.routes]]
    path = "/api/payments"
    upstream = "localhost:8003"

    # Main web application
    [[webserver.routes]]
    path = "/"
    upstream = "localhost:8000"

Example: API Gateway with Path Stripping
-----------------------------------------

Sometimes you want to strip the path prefix before sending to the upstream:

.. code-block:: toml
    :caption: fujin.toml

    [webserver]
    # Requests to /api/v1/* get proxied to localhost:8001/*
    [[webserver.routes]]
    path = "/api/v1"
    upstream = "localhost:8001"
    strip_path = true

    # Without strip_path, /api/v1/users would be sent as /api/v1/users
    # With strip_path = true, /api/v1/users becomes /users

Generated Caddyfile
-------------------

The routes configuration generates a Caddyfile like this:

.. code-block:: caddyfile

    example.com {
        handle_path /static/* {
            root * /var/www/app/static/
            file_server
        }

        handle /ws* {
            reverse_proxy localhost:8001
        }

        handle /* {
            reverse_proxy unix//run/app/gunicorn.sock
        }
    }

Troubleshooting
---------------

Route Order Matters
~~~~~~~~~~~~~~~~~~~

Routes are evaluated in order. Place more specific routes before general ones:

.. code-block:: toml

    # ✓ Correct: specific route first
    [[webserver.routes]]
    path = "/api/admin"
    upstream = "localhost:8001"

    [[webserver.routes]]
    path = "/api"
    upstream = "localhost:8000"

    # ✗ Wrong: general route would catch everything
    # [[webserver.routes]]
    # path = "/api"
    # upstream = "localhost:8000"
    #
    # [[webserver.routes]]
    # path = "/api/admin"
    # upstream = "localhost:8001"

Cannot Use Both upstream and routes
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

If you see this error:

.. code-block:: text

    Webserver cannot have both 'upstream' and 'routes' configured

Choose one approach:

- Use ``upstream`` for simple single-backend setup
- Use ``routes`` for multiple backends with path routing

Must Configure Either upstream or routes
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

If you see:

.. code-block:: text

    Webserver must have either 'upstream' or 'routes' configured

You need to configure at least one backend. Add either:

.. code-block:: toml

    [webserver]
    upstream = "localhost:8000"

Or:

.. code-block:: toml

    [[webserver.routes]]
    path = "/"
    upstream = "localhost:8000"
