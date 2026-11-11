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

Bitwarden and Vaultwarden
-------------------------

The ``fujin-secrets-bitwarden`` plugin resolves refs from a Bitwarden vault
using the ``bw`` CLI. Vaultwarden is Bitwarden-API compatible, so no server-side
change is needed; point the CLI at your instance before logging in:

.. code-block:: bash

    bw config server https://vault.example.com
    bw login
    export BW_SESSION="$(bw unlock --raw)"

Configuration with folder-scoped resolution:

.. code-block:: toml
    :caption: fujin.toml

    [secrets]
    adapter = "bitwarden"

    [secrets.options]
    password_env = "BW_PASSWORD"  # Optional; omit to be prompted
    folders = ["fujin/myapp/prod", "fujin/shared"]
    cache_session = true  # Optional; reuse the session across deploys

If ``BW_SESSION`` is unset and no password is available through
``password_env`` (unset or naming an unset variable), Fujin asks for the
master password once per run and unlocks the vault with ``bw unlock``.
The resulting session is kept in memory for the rest of the process and is
never written to shell history. By default it is not written to disk either.

Set ``cache_session = true`` to keep the session in a per-user runtime file
(``$XDG_RUNTIME_DIR/fujin/bitwarden-session/session``, mode ``0600``) so later
deploys - in the same tab or any other - reuse it without asking again. The
cache lives on tmpfs and is removed at logout, and it is discarded as soon as
Bitwarden rejects the session, so the next deploy prompts again. This is a
convenience, not a security boundary: any process running as the same user can
read the file; use ``BW_SESSION`` or ``password_env`` if you need isolation.

The prompt appears after the deploy
confirmation and before the build starts, so it owns the terminal; the deploy
shows the adapter's progress (unlocking, syncing the vault, reading secrets)
while it waits, so a slow sync never looks stuck. ``fujin deploy --no-input``
disables the prompt (an unattended deploy never blocks), so CI must set
``BW_SESSION`` or ``password_env``.

.. list-table::
    :header-rows: 1

    * - Option
      - Type
      - Description
    * - ``password_env``
      - string
      - Environment variable holding the vault password, used to unlock when
        ``BW_SESSION`` is not set. Optional: when the variable is unset (or
        ``password_env`` is omitted) Fujin prompts for the master password.
    * - ``folders``
      - string or list of strings
      - Folder names to resolve refs from, in lookup order (first match wins).
        A bare string is accepted as a one-element order. An empty list is
        rejected at config load; omit the option to resolve every ref by item
        name across the whole vault.
    * - ``cache_session``
      - boolean
      - Keep the session returned by an interactive unlock in the user's
        runtime directory so later deploys reuse it. Defaults to ``false``.
        The file is mode ``0600``, lives for the login session (tmpfs, removed
        at logout), and is cleared when Bitwarden invalidates the token. Only
        applies to the ``bitwarden`` CLI adapter.

Recommended folder layout — app secrets live under ``fujin/`` so personal
logins cannot collide with app env refs:

.. code-block:: text

    fujin/
    ├── myapp/
    │   ├── prod/          # prod-only secrets
    │   └── staging/
    └── shared/            # cross-app secrets (SMTP, S3, DB URLs)

Resolution order and batching:

* Folder names are resolved to ids once per deploy, then Fujin issues **one**
  ``bw list items --folderid <id> --raw`` call per configured folder. Refs are
  resolved from an in-process ``{item name: password}`` map, so there is no
  ``bw get password`` call on this path.
* ``$DB_PASSWORD`` resolves from ``fujin/myapp/prod`` first, then
  ``fujin/shared``. The first folder containing an item with that exact name
  wins; duplicate item names in later folders are ignored.
* Folder names must be unique in the vault. If ``bw list folders`` returns two
  folders with the same configured name, resolution fails closed and names
  the duplicated folder instead of picking one arbitrarily.
* A ref containing a slash names its folder explicitly:
  ``$fujin/shared/postgres`` resolves only from ``fujin/shared``. This
  disambiguates items that share a name across folders.
* A ref that no folder provides fails closed before anything is uploaded. The
  error names the env key, the ref and the folders that were tried, and never
  echoes resolved values.
* Only **login** items with a password are resolved. Secure notes, cards,
  identities and passwordless logins are skipped.

Without ``folders``, the adapter keeps its historical behaviour: one
concurrent ``bw get password <name> --raw`` per ref, matched by item name
across the whole vault.

Custom Adapters
--------------

You can create your own secret adapter by implementing the adapter interface and registering it via Python entry points.

See the `plugin packages source code <https://github.com/Tobi-De/fujin/tree/main/plugins>`_ for examples of how to implement custom adapters.

Adapters that need to prompt for credentials (for example a vault master
password) must check ``fujin.secrets.prompts_allowed()`` first. It is true by
default and false for the whole process when ``fujin deploy --no-input`` is
used, so an unattended deploy never blocks on a terminal.

When prompting is allowed, resolution runs alone *before* the build, so a
prompt never shares the terminal with streaming build output. Adapters should
wrap ``getpass``/``input`` in ``fujin.secrets.credential_prompt()``: the
deploy's live progress display pauses for the prompt and resumes afterwards.
Report long steps (vault sync, sign-in) with ``fujin.secrets.progress(msg)``;
it updates that display and is a no-op when no display is running (piped
output, unattended runs, other commands).

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
