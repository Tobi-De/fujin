Tutorial
========

In this tutorial, you will deploy a Django project or a PocketBase binary (in binary mode) to a Linux server running Ubuntu using the ``fujin`` CLI.

First, make sure you follow the `installation </installation.html>`_ instructions and have the ``fujin`` command available globally in your shell.

Prerequisites
-------------

Linux Box
*********

``fujin`` has no strict requirements on the virtual private server (VPS) you choose, apart from the fact that it must be running a recent version of Ubuntu or a Debian-based system.
I've mainly run my tests with various versions of Ubuntu: 20.04, 22.04, and 24.04. Other than that, use the best option for your app or the cheapest option you can find and make sure you 
have root access to the server.

Domain name
***********

You can get one from popular registrars like `namecheap <https://www.namecheap.com/>`_ or `godaddy <https://www.godaddy.com>`_. If you just need something to test this tutorial, you can use
`sslip <https://sslip.io/>`_, which is what I'll be using here.

If you've bought a new domain, create an **A record** to point to the server IP address.

Python package
--------------

.. note::

    If you are deploying a binary or self-contained executable, skip to the `next section </tutorial.html#binary>`_

Project Setup
*************

If you are deploying a Python project with ``fujin``, you need your project to be packaged and ideally have an entry point. We will be using Django as an example here, but the same steps
can be applied to any other Python project, and you can find examples with more frameworks in the `examples <https://github.com/falcopackages/fujin/tree/main/examples/>`_ folder on GitHub.

Let's start by installing and initializing a simple Django project.

.. code-block:: shell

    uv tool install django
    django-admin startproject bookstore
    cd bookstore
    uv init --package .
    uv add django gunicorn

The ``uv init --package`` command makes your project mostly ready to be used with ``fujin``. It initializes a `packaged application <https://docs.astral.sh/uv/concepts/projects/#packaged-applications>`_ using uv,
meaning the app can be packaged and distributed (e.g: via PyPI) and defines an entry point, which are the two requirements of ``fujin``.

This is the content you'll get in the **pyproject.toml** file, with the relevant parts highlighted.

.. code-block:: toml
    :caption: fujin.toml
    :linenos:
    :emphasize-lines: 15-16,18-20

    [project]
    name = "bookstore"
    version = "0.1.0"
    description = "Add your description here"
    readme = "README.md"
    authors = [
        { name = "Tobi", email = "tobidegnon@proton.me" }
    ]
    requires-python = ">=3.12"
    dependencies = [
        "django>=5.1.3",
        "gunicorn>=23.0.0",
    ]

    [project.scripts]
    bookstore = "bookstore:main"

    [build-system]
    requires = ["hatchling"]
    build-backend = "hatchling.build"

The *build-system* section is what allows us to build our project into a wheel file (Python package format), and the *project.scripts* defines a CLI entry point for our app.
This means that if our app is installed (either with ``pip install`` or ``uv tool install``, for example), there will be a ``bookstore`` command available globally on our system to run the project.

.. note::

    If you are installing it in a virtual environment, then there will be a file **.venv/bin/bookstore** that will run this CLI entry point. This is what ``fujin`` expects internally.
    When it deploys your Python project, it sets up and installs a virtual environment in the app directory in a **.venv** folder and expects this entry point to be able to run
    commands with the ``fujin app exec <command>`` command.

Currently, our entry point will run the main function in the **src/bookstore/__init__.py** file. Let's change that.

.. code-block:: shell

    rm -r src
    mv manage.py bookstore/__main__.py

We first remove the **src** folder, as we won't use that since our Django project will reside in the top-level **bookstore** folder. I also recommend keeping all
your Django code in that folder, including new apps, as this makes things easier for packaging purposes.
Then we move the **manage.py** file to the **bookstore** folder and rename it to **__main__.py**. This enables us to do this:

.. code-block:: shell

    uv run bookstore migrate # equivalent to python manage.py migrate if we kept the manage.py file

Now to finish, update the *scripts* section in your **pyproject.toml** file.

.. code-block:: toml
    :caption: fujin.toml

    [project.scripts]
    bookstore = "bookstore.__main__:main"

Now the CLI that will be installed with your project will do the job of the **manage.py** file. To test this out, run the following commands:

.. code-block:: shell

    uv sync # needed because we updated the scripts section
    source .venv/bin/activate
    bookstore runserver


.. admonition:: falco
    :class: tip dropdown

    If you want a Django project with all these prerequisites in place, check out `falco <https://github.com/falcopackages/falco-cli>`_.
    It also automatically provides a ``start_app`` command that moves the app to the right folder.

fujin init
**********

Now that our project is ready, run ``fujin init`` at the root of it.

.. admonition:: falco
    :class: tip dropdown

    In a falco project, run ``fujin init --profile falco``

Here's what you'll get:

.. code-block:: toml
    :caption: fujin.toml

    app = "bookstore"
    build_command = "uv build && uv pip compile pyproject.toml -o requirements.txt"
    distfile = "dist/bookstore-{version}-py3-none-any.whl"
    requirements = "requirements.txt"
    release_command = "bookstore migrate"
    installation_mode = "python-package"

    [webserver]
    upstream = "unix//run/bookstore.sock"
    type = "fujin.proxies.caddy"

    [processes]
    web = ".venv/bin/gunicorn bookstore.wsgi:application --bind unix//run/bookstore.sock"

    [aliases]
    shell = "server exec --appenv -i bash"

    [host]
    user = "root"
    domain_name = "bookstore.com"
    envfile = ".env.prod"

Update the host section; it should look something like this, but with your server IP:

.. code-block:: toml
    :caption: fujin.toml

    [host]
    domain_name = "SERVER_IP.sslip.io"
    user = "root"
    envfile = ".env.prod"

.. caution::
    
    Make sure to replace ``SERVER_IP`` with the actual IP address of your server.

Create a **.env.prod** file at the root of your project; it can be an empty file for now. The only requirement is that the file should exist.
Update your **bookstore/settings.py** with the changes below:

.. code-block:: python
    :caption: settings.py

    # SECURITY WARNING: don't run with debug turned on in production!
    DEBUG = False

    ALLOWED_HOSTS = ["SERVER_IP.sslip.io"]

With the current setup, we should already be able to deploy our app with the ``fujin up`` command, but static files won't work. Let's make some changes.

Update **bookstore/settings.py** with the changes below:

.. code-block:: python
    :caption: settings.py
    :linenos:
    :lineno-start: 118
    :emphasize-lines: 119

    STATIC_URL = "static/"
    STATIC_ROOT = "./staticfiles"

The last line means that when the ``collectstatic`` command is run, the files will be placed in a **staticfiles** directory in the current directory.

Now let's update the **fujin.toml** file to run ``collectstatic`` before the app is started and move these files to the folder where our web server
can read them:

.. code-block:: toml

    ...
    release_command = "bookstore migrate && bookstore collectstatic --no-input && sudo rsync --mkpath -a --delete staticfiles/ /var/www/bookstore/static/"
    ...

    [webserver]
    ...
    statics = { "/static/*" = "/var/www/bookstore/static/" }

.. note::

    If your server has a version of rsync that does not have the ``--mkpath`` option, you can update the rsync part to create the folder beforehand:

    .. code-block:: text

        && sudo mkdir -p /var/www/bookstore/static/ && sudo rsync -a --delete staticfiles/ /var/www/bookstore/static/"

Now move to the `create user </tutorial.html#create-user>`_ section for the next step.

Binary
------

This mode is intended for self-contained executables, for example, with languages like Golang or Rust that can be compiled into a single file that is shipped to the server.
You can get a similar feature in Python with tools like `pyapp <https://github.com/ofek/pyapp>`_ and `pex <https://github.com/pex-tool/pex>`_.
For this tutorial, we will use `pocketbase <https://github.com/pocketbase/pocketbase>`_, a Go backend that can be run as a standalone app.

.. code-block:: shell

    mkdir pocketbase
    cd pocketbase
    touch .env.prod
    curl -LO https://github.com/pocketbase/pocketbase/releases/download/v0.22.26/pocketbase_0.22.26_linux_amd64.zip
    fujin init --profile binary

With the instructions above, we will download a version of Pocketbase to run on Linux from their GitHub release and initialize a new fujin configuration in *binary* mode.
Now update the **fujin.toml** file with the changes below:

.. code-block:: toml
    :caption: fujin.toml
    :linenos:
    :emphasize-lines: 2-5,9,13,19-21

    app = "pocketbase"
    version = "0.22.26"
    build_command = "unzip pocketbase_0.22.26_linux_amd64.zip"
    distfile = "pocketbase"
    release_command = "pocketbase migrate"
    installation_mode = "binary"

    [webserver]
    upstream = "localhost:8090"
    type = "fujin.proxies.caddy"

    [processes]
    web = "pocketbase serve --http 0.0.0.0:8090"

    [aliases]
    shell = "server exec --appenv -i bash"

    [host]
    domain_name = "SERVER_IP.sslip.io"
    user = "root"
    envfile = ".env.prod"

.. caution::
    
    Make sure to replace *SERVER_IP* with the actual IP address of your server.

Create User
-----------

Currently, we have the user set to **root** in our **fujin.toml** file and ``fujin`` might work with the root user, but I've noticed some issues with it, so I highly recommend creating a custom user.
For that, you'll need the root user with SSH access set up on the server.
Then you'll run the command ``fujin server create-user`` with the username you want to use. You can, for example, use *fujin* as the username.

.. code-block:: shell
    :caption: create-user example

    fujin server create-user fujin

This will create a new **fujin** user on your server, add it to the ``sudo`` group with the option to run all commands without having to type a password, and will
copy the authorized key from the **root** to your new user so that the SSH setup you made for the root user still works with this new one.
Now update the **fujin.toml** file with the new user:

.. code-block:: toml
    :caption: fujin.toml

    [host]
    domain_name = "SERVER_IP.sslip.io"
    user = "fujin"
    envfile = ".env.prod"

Deploy
------

Now that your project is ready, run the commands below to deploy for the first time:

.. code-block:: shell

    fujin up

The first time, the process can take a few minutes. At the end of it, you should have a link to your deployed app.

.. admonition:: A few notable commands
    :class: note dropdown

    .. code-block:: shell
        :caption: Deploy an app on a host where ``fujin`` has already been set up

        fujin deploy

    You also use the ``deploy`` command when you have changed the ``fujin`` config or exported configs:

    .. code-block:: shell
        :caption: Export the systemd config being used so that you can edit them

        fujin app export-config

    .. code-block:: shell
        :caption: Export the webserver config, in this case, caddy

        fujin proxy export-config

    and the command you'll probably be running the most:

    .. code-block:: shell
        :caption: When you've only made code and envfile related changes

        fujin redeploy

FAQ
---

What about my database?
************************

I'm currently using SQLite for my side projects, so this isn't an issue for me at the moment. That's why ``fujin`` does not currently assist with databases. 
However, you can still SSH into your server and manually install PostgreSQL or any other database or services you need.

I plan to add support for managing additional tools like Redis or databases by declaring containers via the **fujin.toml** file. These containers will be managed with ``podman``,
To follow the development of this feature, subscribe to this `issue <https://github.com/falcopackages/fujin/issues/17>`_.