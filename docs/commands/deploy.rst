deploy
======


.. cappa:: fujin.commands.deploy.Deploy
   :style: terminal
   :terminal-width: 0


How it works
------------

Here's a high-level overview of what happens when you run the ``deploy`` command:

1. **Resolve secrets**: If you have defined a ``secrets`` configuration, it will be used to retrieve pull the ``secrets`` defined in your ``envfile``.

2. **Build the Application**: Your application is built using the ``build_command`` specified in your configuration.

3. **Bundle Artifacts**: All deployment assets are staged locally into a single tarball (``deploy.tar.gz``). The bundle contains your dist file, optional ``requirements.txt``, the rendered ``.env``, generated systemd unit files, the Caddyfile (when enabled), and install/uninstall scripts. A checksum is calculated for integrity verification.

4. **Upload Once**: The bundle is uploaded to the host under ``{app_dir}/.versions/`` and its checksum is verified remotely. Retries are offered on mismatch.

5. **Install from Bundle**: The bundle is extracted on the host and ``install.sh`` installs the project: Python mode creates/uses the virtualenv and installs dependencies; binary mode copies the binary into place. If configured, ``release_command`` runs after installation.

6. **Configure and Start Services**: Generated ``systemd`` unit files are installed (stale ones are cleaned up), services are enabled and restarted, and the Caddy configuration is applied/reloaded when enabled.  

7. **Prune Old Bundles**: Old bundles are removed from ``.versions`` according to ``versions_to_keep``.

8. **Completion**: A success message is displayed, and the URL to access the deployed project is provided.

Below is an example of the layout and structure of a deployed application:

.. tab-set::

    .. tab-item:: python package

        .. code-block:: shell

            app_directory/
            ├── .env                              # Environment variables file
            ├── .appenv                           # Application-specific environment setup
            ├── .version                          # Current deployed version
            ├── .venv/                            # Virtual environment
            └── .versions/                        # Stored deployment bundles
                ├── app-1.2.3.tar.gz
                └── app-1.2.2.tar.gz

    .. tab-item:: binary

        .. code-block:: shell

            app_directory/
            ├── .env                              # Environment variables file
            ├── .appenv                           # Application-specific environment setup
            ├── .version                          # Current deployed version
            ├── app_binary                        # Installed binary
            └── .versions/                        # Stored deployment bundles
                ├── app-1.2.3.tar.gz
                └── app-1.2.2.tar.gz