Hooks
=====

Hooks allow you to run scripts at specific points in the deployment process. These scripts can perform checks before deployment, run tests, or send a notification after a successful deployment.

The currently available ``hooks`` are:

- ``pre_build``: The first action executed when the ``deploy`` command is run, before your app is built.
- ``pre_deploy``: Executed after app is built before the code is pushed to the remote server.
- ``post_deploy``: Executed after a successful deployment.

.. note::

    Hooks are run on your local machine, not on the remote server. If they fail, the deployment process is stopped.

You can specify hooks in the ``fujin.toml`` file like this:

.. code-block::

    [hooks]
    pre_build = "echo 'hello'"

.. tip::

    Using a `justfile <https://just.systems/>`_ can be an easy way to write more complex scripts without adding new files.

    .. code-block::

        [hooks]
        pre_build = "just run-some-checks"

Alternatively, you can create a file with the hook's name in the ``.fujin/hooks`` folder and ensure the file is executable.

.. code-block::
    :caption: .fujin/hooks/pre_build

    #!/usr/bin/env bash

    echo 'hello'

The ``hooks`` configuration takes precedence over files in the ``.fujin/hooks`` folder; you cannot mix and match both.