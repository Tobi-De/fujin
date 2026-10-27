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

Secret References and Escaping
------------------------------

A few rules apply to every secret adapter and to plain ``.env`` files without
one:

* ``$REF`` (a value starting with a single ``$``) is a secret reference. The
  ref runs from the ``$`` to the end of the line: ``API_KEY=$MY_REF`` asks the
  adapter for ``MY_REF``. Everything after the ``$`` on that line is part of
  the ref name, so trailing text is not "appended" to the value.
* ``$$`` escapes a literal ``$``. ``PASSWORD=pa$$word`` deploys ``pa$word``,
  ``DOLLAR=$$`` deploys ``$`` and ``PATTERN=$${HOME}`` deploys the literal
  text ``${HOME}`` (it is not a secret reference and is not expanded). The
  ``$$`` check happens *before* secret-ref parsing.
* Beyond that, env files keep python-dotenv's normal interpolation:
  ``${VAR}`` is expanded as before. Use ``$$`` when you need a literal ``$``
  next to a brace expression.
* An unresolved ``$REF`` is fail-closed. If the configured adapter (or, with
  no adapter configured at all) does not produce a value, the deploy aborts
  before anything is uploaded, naming only the key and the ref. If no secret
  adapter is configured for the host, use ``$$`` for a literal ``$`` instead
  of ``$REF``.

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

   Resolved secret values are registered for redaction at deploy time: in
   fujin's **own** output — command messages, log records, exceptions and
   the audit trail — any registered value is replaced with a ``<$REF>``
   placeholder instead of the real bytes. The env file that is signed into
   a release still contains the real values (that is its purpose).

   The scrubbing covers what fujin emits, not everything it touches. By
   design, secret values are still plainly visible in these places:

   - ``fujin show env`` (and ``--plain``), and the rendered ``.env`` on
     the server, are meant to show real values.
   - Hook output: hooks run remotely with the release environment sourced
     (``set -a``) and their output streams over the SSH pty. A hook that
     echoes a secret prints it as-is — hooks are *supposed* to be able to
     see the values.
   - Application runtime logs (anything the app itself prints).

   Resolution is fail-closed, before any server contact: a missing
   reference, a failing adapter or a secret that the adapter left
   unresolved aborts the deploy with a nonzero exit code and nothing is
   uploaded.
