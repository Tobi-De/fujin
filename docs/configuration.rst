Configuration
=============

Fujin uses a **fujin.toml** file at the root of your project for configuration. Below are all available configuration options.

app
---
The name of your project or application. Must be a valid Python package name.

version
--------
The version of your project to build and deploy. If not specified, automatically parsed from **pyproject.toml** under *project.version*.

python_version
--------------
The Python version for your virtualenv. If not specified, automatically parsed from **.python-version** file. This is only
required if the installation mode is set to **python-package**

requirements
------------
Optional path to your requirements file. This will only be used when the installation mode is set to *python-package*

versions_to_keep
----------------
The number of versions to keep on the host. After each deploy, older versions are pruned based on this setting. By default, it keeps the latest 5 versions,
set this to `None` to never automatically prune.

build_command
-------------
The command to use to build your project's distribution file.

distfile
--------
Path to your project's distribution file. This should be the main artifact containing everything needed to run your project on the server.
Supports version placeholder, e.g., **dist/app_name-{version}-py3-none-any.whl**

installation_mode
-----------------

Indicates whether the *distfile* is a Python package or a self-contained executable. The possible values are *python-package* and *binary*.
The *binary* option disables specific Python-related features, such as virtual environment creation and requirements installation. ``fujin`` will assume the provided
*distfile* already contains all the necessary dependencies to run your program.

release_command
---------------
Optional command to run at the end of deployment (e.g., database migrations) before your application is started.

secrets
-------

Optional secrets configuration. If set, ``fujin`` will load secrets from the specified secret management service.
Check out the `secrets </secrets.html>`_ page for more information.

adapter
~~~~~~~
The secret management service to use. The currently available options are *bitwarden*, *1password*, *doppler*

password_env
~~~~~~~~~~~~
Environment variable containing the password for the service account. This is only required for certain adapters.

Sites
-----

Caddy web server routing configuration. Each site defines one or more domains and how traffic should be routed.

domains
~~~~~~~
List of domain names this site will serve. Used for Caddy reverse proxy configuration and SSL certificate generation.

.. code-block:: toml
    :caption: fujin.toml

    [[sites]]
    domains = ["example.com", "www.example.com"]

routes
~~~~~~

Defines how incoming requests are routed based on URL paths. Routes are evaluated in order (top-to-bottom), so place more specific routes first.

**Route Types:**

- **Process route** (string): Route to a process by name. The process must have a ``listen`` field.
- **Process route with options** (dict): Route to a process with additional options like ``strip_prefix``.
- **Static files** (dict): Serve static files from a directory using ``{ static = "path" }``.

**Route Syntax:**

Simple process route:

.. code-block:: toml

    routes = { "/" = "web" }

Process route with path stripping:

.. code-block:: toml

    routes = { "/api/*" = { process = "api", strip_prefix = "/api" } }

Static files route:

.. code-block:: toml

    routes = { "/static/*" = { static = "/var/www/app/static/" } }

**Variable Interpolation:**

Static file paths support variable interpolation using Python's ``str.format()`` syntax. Available variables:

- ``{app_name}`` - Your application name
- ``{app_dir}`` - Full path to application directory
- ``{user}`` - Host user

**Complete Example:**

.. code-block:: toml
    :caption: fujin.toml

    [[sites]]
    domains = ["example.com", "www.example.com"]
    routes = {
        "/ws/*" = "websocket",                          # WebSocket to daphne
        "/api/*" = { process = "api", strip_prefix = "/api" },  # API with path stripping
        "/static/*" = { static = "/var/www/{app_name}/static/" },
        "/media/*" = { static = "/var/www/{app_name}/media/" },
        "/" = "web"                                      # Default to main process
    }

**Multiple Sites:**

You can define multiple sites for different domains:

.. code-block:: toml
    :caption: fujin.toml

    [[sites]]
    domains = ["example.com"]
    routes = { "/" = "web" }

    [[sites]]
    domains = ["api.example.com"]
    routes = { "/" = "api" }

processes
---------

A mapping of process names to their configuration. This section serves as the **metadata** that drives the generation of Systemd unit files.
Fujin uses a template-based approach where the data defined here is passed to Jinja2 templates to render the final service files.

Each entry in the `processes` dictionary represents a service that will be managed by Systemd. The key is the process name (e.g., `web`, `worker`), and the value is a dictionary of configuration options.

**Configuration Options:**

- **command** (required): The command to execute. Relative paths are resolved against the application directory on the host.
- **listen** (optional): The address where this process listens for requests. Required if the process is referenced in site routes. Supports:

  - HTTP address (e.g., ``localhost:8000``)
  - Unix socket (e.g., ``unix//run/project.sock``)

- **replicas** (optional, default: 1): The number of instances to run. If > 1, a template unit (e.g., `app-worker@.service`) is generated. Cannot be used with ``listen`` (which replica would Caddy route to?).
- **socket** (optional, default: false): If true, enables socket activation. Fujin will look for a corresponding socket template.
- **timer** (optional): Configuration for systemd timer-based scheduling. Accepts a dictionary with the following options:

  - **on_calendar**: Calendar event expression (e.g., ``"daily"``, ``"*:*:00"`` for every minute)
  - **on_boot_sec**: Time to wait after system boot (e.g., ``"5m"`` for 5 minutes)
  - **on_unit_active_sec**: Time to wait after the service was last active (e.g., ``"1h"`` for recurring tasks)
  - **on_active_sec**: Time to wait after the timer was activated
  - **persistent** (default: true): Whether to catch up on missed runs
  - **randomized_delay_sec**: Random delay to add (useful to prevent thundering herd)
  - **accuracy_sec**: Timer accuracy (can save power on low-precision timers)

  At least one trigger (``on_calendar``, ``on_boot_sec``, ``on_unit_active_sec``, or ``on_active_sec``) must be specified.

**Template Selection Logic:**

For each process defined, Fujin looks for a matching template in your local configuration directory (default: `.fujin/`) or falls back to the built-in defaults.
The lookup order for a process named `worker` is:

1.  `worker.service.j2` (Specific template)
2.  `default.service.j2` (Generic fallback)

This allows you to have a generic configuration for most processes while customizing specific ones (like `web`) by simply creating a `web.service.j2` file.

Example:

.. code-block:: toml
    :caption: fujin.toml

    # Web process - routable via Caddy
    [processes.web]
    command = ".venv/bin/gunicorn myproject.wsgi:application --bind unix:/run/myapp/web.sock"
    listen = "unix//run/myapp/web.sock"

    # WebSocket process - routable via Caddy
    [processes.websocket]
    command = ".venv/bin/daphne -u /run/myapp/ws.sock myproject.asgi:application"
    listen = "unix//run/myapp/ws.sock"

    # Background worker - not routable (no listen field)
    [processes.worker]
    command = ".venv/bin/celery -A myproject worker"
    replicas = 2

    # Simple timer - run daily
    [processes.beat]
    command = ".venv/bin/celery -A myproject beat"
    timer = { on_calendar = "daily" }

    # Advanced timer - run hourly with randomized delay to prevent thundering herd
    [processes.cleanup]
    command = ".venv/bin/cleanup"
    timer = { on_calendar = "hourly", randomized_delay_sec = "5m" }

    # Run 5 minutes after boot, then every hour after last completion
    [processes.health]
    command = ".venv/bin/healthcheck"
    timer = { on_boot_sec = "5m", on_unit_active_sec = "1h" }


.. note::

    When generating systemd service files, the full path to the command is automatically constructed based on the *apps_dir* setting.
    You can inspect the default templates in the source code or by running `fujin init --templates` to copy them to your project.

Host Configuration
-------------------

Fujin supports deploying to multiple hosts (servers) from a single configuration file. This is useful for managing staging and production environments, or deploying to multiple servers.

**Single Host Setup:**

.. code-block:: toml

   [[hosts]]
   address = "example.com"
   user = "deploy"
   envfile = ".env.prod"

**Multi-Host Setup:**

.. code-block:: toml

   [[hosts]]
   name = "staging"
   address = "staging.example.com"
   user = "deploy"
   envfile = ".env.staging"

   [[hosts]]
   name = "production"
   address = "example.com"
   user = "deploy"
   envfile = ".env.prod"

.. important::

   When using multiple hosts, each host **must** have a unique ``name`` field. Use the ``-H`` flag to target specific hosts:

   .. code-block:: bash

      fujin deploy -H production
      fujin app logs -H staging

   Without ``-H``, commands target the first host by default.

Host Fields
~~~~~~~~~~~

name
^^^^

**(Required for multi-host setups)**

Unique identifier for the host. Use this with the ``-H`` flag to target specific hosts.

.. code-block:: toml

   [[hosts]]
   name = "production"  # Required when you have multiple hosts

address
^^^^^^^

**(Required)**

The IP address or hostname to connect to via SSH. This is purely for SSH connection purposes.

.. code-block:: toml

   [[hosts]]
   address = "192.168.1.100"  # Connect via IP

   [[hosts]]
   address = "example.com"    # Connect via hostname

.. note::

   Domain names for Caddy/SSL are now configured in the ``sites`` section, not in host configuration.

user
^^^^

**(Required)**

The login user for running remote tasks. Should have passwordless sudo access for optimal operation.

.. note::

    You can create a user with these requirements using the ``fujin server create-user`` command.

envfile
^^^^^^^

**(Optional)**

Path to the production environment file that will be copied to the host.

.. code-block:: toml

   [[hosts]]
   envfile = ".env.prod"

env
^^^

**(Optional)**

A string containing the production environment variables. In combination with the secrets manager, this is most useful when
you want to automate deployment through a CI/CD platform like GitLab CI or GitHub Actions. For an example of how to do this,
check out the `integrations guide </integrations.html>`_

.. code-block:: toml

   [[hosts]]
   env = """
   DEBUG=False
   SECRET_KEY=$SECRET_KEY
   DATABASE_URL=$DATABASE_URL
   """

.. important::

    *envfile* and *env* are mutually exclusive—you can define only one.

apps_dir
^^^^^^^^

**(Optional, default: .local/share/fujin)**

Base directory for project storage on the host. Path is relative to user's home directory unless it starts with ``/``.

This value determines your project's **app_dir**, which is **{apps_dir}/{app}**.

.. code-block:: toml

   [[hosts]]
   apps_dir = "/opt/apps"  # Absolute path
   # Results in: /opt/apps/myapp

   [[hosts]]
   apps_dir = ".local/share/fujin"  # Relative to home
   # Results in: /home/user/.local/share/fujin/myapp

password_env
^^^^^^^^^^^^

**(Optional)**

Environment variable containing the user's password. Only needed if the user cannot run sudo without a password.

.. code-block:: toml

   [[hosts]]
   password_env = "DEPLOY_PASSWORD"

port
^^^^

**(Optional, default: 22)**

SSH port for connecting to the host.

.. code-block:: toml

   [[hosts]]
   port = 2222

key_filename
^^^^^^^^^^^^

**(Optional)**

Path to the SSH private key file for authentication. Optional if using your system's default key location.

.. code-block:: toml

   [[hosts]]
   key_filename = "~/.ssh/deploy_key"

key_passphrase_env
^^^^^^^^^^^^^^^^^^

**(Optional)**

Environment variable containing the SSH key passphrase if your key is encrypted.

.. code-block:: toml

   [[hosts]]
   key_filename = "~/.ssh/deploy_key"
   key_passphrase_env = "SSH_KEY_PASSPHRASE"

aliases
-------

A mapping of shortcut names to Fujin commands. Allows you to create convenient shortcuts for commonly used commands.

Example:

.. code-block:: toml
    :caption: fujin.toml

    [aliases]
    console = "app exec -i shell_plus" # open an interactive django shell
    dbconsole = "app exec -i dbshell" # open an interactive django database shell
    shell = "server exec --appenv -i bash" # SSH into the project directory with environment variables loaded


Example
-------

This is a minimal working example.

.. tab-set::

    .. tab-item:: python package

        .. exec_code::
            :language_output: toml

            # --- hide: start ---
            from fujin.commands.init import simple_config
            from tomli_w import dumps

            print(dumps(simple_config("bookstore"),  multiline_strings=True))
            #hide:toggle

    .. tab-item:: binary mode

        .. exec_code::
            :language_output: toml

            # --- hide: start ---
            from fujin.commands.init import binary_config
            from tomli_w import dumps

            print(dumps(binary_config("bookstore"),  multiline_strings=True))
            #hide:toggle
