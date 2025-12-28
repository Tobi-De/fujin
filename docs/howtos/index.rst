How-tos
=======

This section contains practical, step-by-step guides for deploying applications with Fujin.

.. toctree::
   :maxdepth: 1
   :caption: Guides

   django
   binary

Prerequisites
-------------

Linux Box
*********

``fujin`` has no strict requirements on the virtual private server (VPS) you choose, apart from the fact that it must be running a recent version of Ubuntu or a Debian-based system.
I've mainly run my tests with various versions of Ubuntu: 20.04, 22.04, and 24.04. Other than that, use the best option for your app or the cheapest option you can find.

**SSH Access**: You must have root access to the server via SSH.
- If you are using a cloud provider (DigitalOcean, Hetzner, AWS, etc.), you usually add your public SSH key during the server creation process.
- Alternatively, you might be given a root password. Fujin can work with password-based authentication, but SSH keys are recommended for security and convenience.

Domain name
***********

You can get one from popular registrars like `namecheap <https://www.namecheap.com/>`_ or `godaddy <https://www.godaddy.com>`_. If you just need something to test this tutorial, you can use
`sslip <https://sslip.io/>`_, which is what I'll be using here.

If you've bought a new domain, create an **A record** to point to the server IP address.

Server Setup
------------

It is highly recommended to create a dedicated user for deployment instead of using ``root``. This improves security by limiting the privileges of the application.

The process involves three steps:

1.  **Configure Fujin to use root**: Initially, Fujin needs to connect as root to create the new user.
    Ensure your ``fujin.toml`` has the root user configured:

    .. code-block:: toml

        [host]
        user = "root"
        domain_name = "your-domain.com"

2.  **Create the user**: Run the following command to create a new user (e.g., ``fujin``) on the server.
    This command creates the user, grants passwordless sudo access, and copies your SSH keys from the root user to the new user.

    .. code-block:: shell

        fujin server create-user fujin

3.  **Switch to the new user**: Update your ``fujin.toml`` to use the newly created user for all future operations.

    .. code-block:: toml

        [host]
        user = "fujin"
        domain_name = "your-domain.com"

Now you are ready to deploy your application using this dedicated user.

Common Operations
-----------------

-   **fujin up**: Provisions the server (installs dependencies) and deploys the app. Use this for the first deploy.
-   **fujin deploy**: Updates configuration files (Systemd, Caddy) and the application code. Use this if you changed ``fujin.toml`` or templates.

FAQ
---

What about my database?
***********************

Fujin currently focuses on application deployment. For databases, you can:

1.  Manually install PostgreSQL/MySQL on your server.
2.  Use a managed database service.
3.  Wait for upcoming container support (see `Issue #17 <https://github.com/falcopackages/fujin/issues/17>`_).
