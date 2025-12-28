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

Webserver
---------

Caddy web server configurations.

upstream
~~~~~~~~
The address where your web application listens for requests. Supports any value compatible with your chosen web proxy:

- HTTP address (e.g., *localhost:8000* )
- Unix socket caddy (e.g., *unix//run/project.sock* )

config_dir
~~~~~~~~~~
The directory where the Caddyfile for the project will be stored on the host. Default: **/etc/caddy/conf.d/**

statics
~~~~~~~

Defines the mapping of URL paths to local directories for serving static files. The directories you map should be accessible by caddy, meaning
with read permissions for the *www-data* group; a reliable choice is **/var/www**.

Example:

.. code-block:: toml
    :caption: fujin.toml

    [webserver]
    upstream = "unix//run/project.sock"
    statics = { "/static/*" = "/var/www/myproject/static/" }

processes
---------

A mapping of process names to their configuration. This section serves as the **metadata** that drives the generation of Systemd unit files.
Fujin uses a template-based approach where the data defined here is passed to Jinja2 templates to render the final service files.

Each entry in the `processes` dictionary represents a service that will be managed by Systemd. The key is the process name (e.g., `web`, `worker`), and the value is a dictionary of configuration options.

**Configuration Options:**

- **command** (required): The command to execute. Relative paths are resolved against the application directory on the host.
- **replicas** (optional, default: 1): The number of instances to run. If > 1, a template unit (e.g., `app-worker@.service`) is generated.
- **socket** (optional, default: false): If true, enables socket activation. Fujin will look for a corresponding socket template.
- **timer** (optional): A systemd calendar event expression (e.g., `OnCalendar=daily`). If set, a timer unit is generated instead of a standard service.

**Template Selection Logic:**

For each process defined, Fujin looks for a matching template in your local configuration directory (default: `.fujin/`) or falls back to the built-in defaults.
The lookup order for a process named `worker` is:

1.  `worker.service.j2` (Specific template)
2.  `default.service.j2` (Generic fallback)

This allows you to have a generic configuration for most processes while customizing specific ones (like `web`) by simply creating a `web.service.j2` file.

Example:

.. code-block:: toml
    :caption: fujin.toml

    [processes]
    # Uses web.service.j2 if it exists, otherwise default.service.j2
    web = { command = ".venv/bin/gunicorn myproject.wsgi:application" }

    # Uses default.service.j2, generating a template unit for multiple instances
    worker = { command = ".venv/bin/celery -A myproject worker", replicas = 2 }

    # Uses beat.service.j2 if exists, or default.service.j2. Also generates a timer unit.
    beat = { command = ".venv/bin/celery -A myproject beat", timer = "OnCalendar=daily" }


.. note::

    When generating systemd service files, the full path to the command is automatically constructed based on the *apps_dir* setting.
    You can inspect the default templates in the source code or by running `fujin init --templates` to copy them to your project.

Host Configuration
-------------------

ip
~~
The IP address or anything that resolves to the remote host IP's. This is use to communicate via ssh with the server, if omitted it's value will default to the one of the *domain_name*.

domain_name
~~~~~~~~~~~
The domain name pointing to this host. Used for web proxy configuration.

user
~~~~
The login user for running remote tasks. Should have passwordless sudo access for optimal operation.

.. note::

    You can create a user with these requirements using the ``fujin server create-user`` command.

envfile
~~~~~~~
Path to the production environment file that will be copied to the host.

env
~~~
A string containing the production environment variables. In combination with the secrets manager, this is most useful when
you want to automate deployment through a CI/CD platform like GitLab CI or GitHub Actions. For an example of how to do this,
check out the `integrations guide </integrations.html>`_

.. important::

    *envfile* and *env* are mutually exclusive—you can define only one.

apps_dir
~~~~~~~~

Base directory for project storage on the host. Path is relative to user's home directory.
Default: **.local/share/fujin**. This value determines your project's **app_dir**, which is **{apps_dir}/{app}**.

password_env
~~~~~~~~~~~~

Environment variable containing the user's password. Only needed if the user cannot run sudo without a password.

ssh_port
~~~~~~~~

SSH port for connecting to the host. Default to **22**.

key_filename
~~~~~~~~~~~~

Path to the SSH private key file for authentication. Optional if using your system's default key location.

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
