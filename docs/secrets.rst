Secrets
=======

Fujin supports multiple secret management adapters for secure handling of sensitive data during deployment.

System
------

The system adapter uses environment variables directly from your system. This is the simplest adapter and requires no additional setup.

Update your fujin.toml file with the following configuration:

.. code-block:: toml
    :caption: fujin.toml

    [secrets]
    adapter = "system"

.. code-block:: text
    :caption: Example of an environment file with system environment variables

    DEBUG=False
    AWS_ACCESS_KEY_ID=$AWS_ACCESS_KEY_ID
    AWS_SECRET_ACCESS_KEY=$AWS_SECRET_ACCESS_KEY

The system adapter will look for environment variables with the same name as the value after the $ sign.

Plugin Adapters
--------------

Additional secret adapters are available as separate plugin packages:

- `fujin-secrets-bitwarden <https://pypi.org/project/fujin-secrets-bitwarden/>`_ - Bitwarden vault integration
- `fujin-secrets-1password <https://pypi.org/project/fujin-secrets-1password/>`_ - 1Password CLI integration
- `fujin-secrets-doppler <https://pypi.org/project/fujin-secrets-doppler/>`_ - Doppler secrets management

Custom Adapters
--------------

You can create your own secret adapter by implementing the adapter interface and registering it via Python entry points.

See the `plugin packages source code <https://github.com/Tobi-De/fujin/tree/main/plugins>`_ for examples of how to implement custom adapters.

Redaction and Fail-Closed Resolution
------------------------------------

.. tip:: Redaction and fail-closed resolution

   Resolved secret values are registered for redaction at deploy time: any
   secret value that would appear in command output, log messages or error
   text is replaced with a ``<$REF>`` placeholder instead. The env file that
   is signed into a release still contains the real values (that is its
   purpose); only fujin's own output is scrubbed.

   Resolution is fail-closed, before any server contact: a missing
   reference, a failing adapter or a secret that the adapter left
   unresolved aborts the deploy with a nonzero exit code and nothing is
   uploaded.
