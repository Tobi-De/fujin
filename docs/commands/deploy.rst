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

3. **Transfer Files**: The environment variables file (``.env``) and the distribution file are transferred to the remote server. Optionally transfers ``requirements`` file (if specified).

4. **Install the Project**: Depending on the installation mode (Python package or binary), the project is installed on the remote server. For a Python package, a virtual environment is set up, dependencies are installed, and the distribution file (the wheel file) is then installed. For a binary, the binary file for the latest version is linked to the root of the application directory.

5. **Application Release**: If a ``release_command`` is specified in the configuration, it is executed at this stage. 

6. **Configure and Start Services**: Configuration files for both ``systemd`` and the ``proxy`` (e.g., Caddy, by default) are generated or copied if previously exported. These configuration files are moved to their appropriate directories. A configuration reload is performed, and all relevant services are restarted.  

7. **Update Version History**: The deployed version is recorded in the ``.versions`` file on the remote server.

8. **Prune Old Assets**: Old versions of the application are removed based on the ``versions_to_keep`` configuration.

9. **Completion**: A success message is displayed, and the URL to access the deployed project is provided.

Below is an example of the layout and structure of a deployed application:

.. tab-set::

    .. tab-item:: python package

        .. code-block:: shell

            app_directory/
            ├── .env                              # Environment variables file
            ├── .appenv                           # Application-specific environment setup
            ├── .versions                         # Version tracking file
            ├── .venv/                            # Virtual environment
            ├── v1.2.3/                           # Versioned asset directory
            │   ├── app-1.2.3-py3-none-any.whl    # Distribution file
            │   └── requirements.txt              # Optional requirements file
            ├── v1.2.2/
            │   └── ...
            └── v1.2.1/
                └── ...

    .. tab-item:: binary

        .. code-block:: shell

            app_directory/
            ├── .env                              # Environment variables file
            ├── .appenv                           # Application-specific environment setup
            ├── .versions                         # Version tracking file
            ├── app_binary -> v1.2.3/app_binary   # Symbolic link to current version
            ├── v1.2.3/                           # Versioned asset directory
            │   └── app_binary                    # Distribution file
            ├── v1.2.2/
            │   └── ...
            └── v1.2.1/
                └── ...