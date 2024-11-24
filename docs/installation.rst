Installation
============

If you have `uv <https://docs.astral.sh/uv/getting-started/installation/>`_ installed, run:

.. code-block:: shell

    uv tool install fujin-cli

If not, download a binary version from the GitHub `releases page <https://github.com/falcopackages/fujin/releases>`_ and move the downloaded file
to a directory in your PATH.

Here is an example of how to install the binary on x86 macOS:

.. code-block:: shell

    curl -LsSfO https://github.com/falcopackages/fujin/releases/download/v0.6.0/fujin_cli-x86_64-osx
    chmod +x fujin_cli-x86_64-osx
    mv fujin_cli-x86_64-osx /usr/local/bin/fujin

.. note::

    If you install ``fujin`` using the binary file, you can keep it up to date with ``fujin self update``.