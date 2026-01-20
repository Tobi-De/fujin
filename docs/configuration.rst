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

replicas
--------

A mapping of service names to their replica count. Used for systemd template units (services with ``@`` in their names).

When a service has replicas > 1, Fujin creates a template unit (e.g., ``myapp-worker@.service``) and enables instances 1 through N.

.. code-block:: toml
    :caption: fujin.toml

    [replicas]
    worker = 3  # Creates myapp-worker@1, myapp-worker@2, myapp-worker@3

Systemd Units
-------------

Fujin uses a file-based approach for systemd configuration. Instead of defining processes in ``fujin.toml``, you create actual systemd unit files in the ``.fujin/systemd/`` directory.

**Directory Structure:**

.. code-block:: text

    .fujin/
    ├── Caddyfile              # Caddy reverse proxy configuration
    └── systemd/
        ├── web.service        # Web server service
        ├── worker.service     # Background worker service
        ├── cleanup.service    # Timer-triggered cleanup task
        ├── cleanup.timer      # Timer for cleanup task
        ├── common.d/          # Dropins applied to ALL services
        │   └── limits.conf
        └── web.service.d/     # Dropins for specific service
            └── memory.conf

**Service Files:**

Create standard systemd service files with Fujin-specific variable substitution:

.. code-block:: ini
    :caption: .fujin/systemd/web.service

    [Unit]
    Description=Web Service
    After=network.target

    [Service]
    Type=simple
    User={app_user}
    Group=fujin
    WorkingDirectory={app_dir}
    EnvironmentFile={app_dir}/.env
    ExecStart={app_dir}/.venv/bin/gunicorn myapp.wsgi:app --bind localhost:8000

    [Install]
    WantedBy=multi-user.target

**Available Variables:**

- ``{app_name}`` - Your application name
- ``{app_dir}`` - Full path to application directory (``/opt/fujin/{app_name}``)
- ``{app_user}`` - User to run the app as (defaults to app_name)

**Timers:**

For scheduled tasks, create both a ``.service`` and ``.timer`` file:

.. code-block:: ini
    :caption: .fujin/systemd/cleanup.timer

    [Unit]
    Description=Run cleanup daily

    [Timer]
    OnCalendar=daily
    Persistent=true

    [Install]
    WantedBy=timers.target

**Dropins:**

- ``common.d/*.conf`` - Applied to all services
- ``{service}.service.d/*.conf`` - Applied to specific service

Use the ``fujin new`` command to generate these files:

.. code-block:: bash

    fujin new service worker      # Create a new service file
    fujin new timer cleanup       # Create a timer with its service
    fujin new dropin resources    # Create a common dropin

Caddyfile
---------

Fujin uses Caddy as the reverse proxy. Configure it by creating a ``.fujin/Caddyfile``:

.. code-block:: text
    :caption: .fujin/Caddyfile

    example.com {
        reverse_proxy localhost:8000
    }

For static files (Django example):

.. code-block:: text
    :caption: .fujin/Caddyfile

    example.com {
        handle_path /static/* {
            root * {app_dir}/staticfiles/
            file_server
        }

        handle {
            reverse_proxy localhost:8000
        }
    }

The ``{app_dir}`` variable is substituted during deployment.

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

The IP address or hostname to connect to via SSH.

.. code-block:: toml

   [[hosts]]
   address = "192.168.1.100"  # Connect via IP

   [[hosts]]
   address = "example.com"    # Connect via hostname

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
    console = "app exec shell_plus" # open an interactive django shell
    dbconsole = "app exec dbshell" # open an interactive django database shell
    shell = "server exec --appenv bash" # SSH into the project directory with environment variables loaded


Complete Example
----------------

This is a minimal working example for a Python web application:

.. code-block:: toml
    :caption: fujin.toml

    app = "myapp"
    build_command = "uv build && uv pip compile pyproject.toml -o requirements.txt > /dev/null"
    distfile = "dist/myapp-{version}-py3-none-any.whl"
    requirements = "requirements.txt"
    installation_mode = "python-package"

    [aliases]
    shell = "server exec --appenv bash"
    status = "app info"
    logs = "app logs"
    restart = "app restart"

    [[hosts]]
    user = "deploy"
    address = "myapp.com"
    envfile = ".env.prod"

.. code-block:: ini
    :caption: .fujin/systemd/web.service

    [Unit]
    Description=Web Service
    After=network.target

    [Service]
    Type=simple
    User={app_user}
    Group=fujin
    WorkingDirectory={app_dir}
    EnvironmentFile={app_dir}/.env
    ExecStart={app_dir}/.venv/bin/gunicorn myapp.wsgi:app --bind localhost:8000

    NoNewPrivileges=true
    PrivateTmp=true
    ProtectSystem=strict
    ProtectHome=read-only
    ReadWritePaths={app_dir}

    [Install]
    WantedBy=multi-user.target

.. code-block:: text
    :caption: .fujin/Caddyfile

    myapp.com {
        reverse_proxy localhost:8000
    }
