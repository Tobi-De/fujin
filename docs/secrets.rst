Secrets  
=======  

System
------

The system adapter uses environment variables directly from your system. This is the simplest adapter and requires no additional setup.
Update your fujin.toml file with the following configuration:

[secrets]
adapter = "system"

.. code-block:: text
    :caption: Example of an environment file with system environment variables

    DEBUG=False
    AWS_ACCESS_KEY_ID=$AWS_ACCESS_KEY_ID
    AWS_SECRET_ACCESS_KEY=$AWS_SECRET_ACCESS_KEY

The system adapter will look for environment variables with the same name as the value after the $ sign.

Bitwarden  
---------  

First, download and install the `Bitwarden CLI <https://bitwarden.com/help/cli/#download-and-install>`_. Make sure to log in to your account.  
You should be able to run ``bw get password <name_of_secret>`` and get the value for the secret. This is the command that will be executed when pulling your secrets.  

Add the following to your **fujin.toml** file:

.. code-block:: toml
    :caption: fujin.toml

    [secrets]  
    adapter = "bitwarden"  
    password_env = "BW_PASSWORD"  

To unlock the Bitwarden vault, the password is required. Set the *BW_PASSWORD* environment variable in your shell.
When ``fujin`` signs in, it will always sync the vault first.

Alternatively, you can set the *BW_SESSION* environment variable. If *BW_SESSION* is present, ``fujin`` will use it directly without signing in or syncing the vault. In this case, the *password_env* configuration is not required.

.. code-block:: text  
    :caption: Example of an environment file with Bitwarden secrets  

    DEBUG=False  
    AWS_ACCESS_KEY_ID=$AWS_ACCESS_KEY_ID  
    AWS_SECRET_ACCESS_KEY=$AWS_SECRET_ACCESS_KEY  

Note the *$* sign, which indicates to ``fujin`` that it is a secret.

1Password  
---------  

Download and install the `1Password CLI <https://developer.1password.com/docs/cli>`_, and sign in to your account.  
You need to be actively signed in for Fujin to work with 1Password.  

Update your **fujin.toml** file with the following configuration:

.. code-block:: toml
    :caption: fujin.toml

    [secrets]  
    adapter = "1password"  

.. code-block:: text  
    :caption: Example of an environment file with 1Password secrets  

    DEBUG=False  
    AWS_ACCESS_KEY_ID=$op://personal/aws-access-key-id/password  
    AWS_SECRET_ACCESS_KEY=$op://personal/aws-secret-access-key/password

Doppler
-------

Download and install the `Doppler CLI <https://docs.doppler.com/docs/cli>`_, and sign in to your account.
Move to your project root directory and run ``doppler setup`` to configure your project.

Update your **fujin.toml** file with the following configuration:

.. code-block:: toml
    :caption: fujin.toml

    [secrets]
    adapter = "doppler"

.. code-block:: text
    :caption: Example of an environment file with doppler secrets

    DEBUG=False
    AWS_ACCESS_KEY_ID=$AWS_ACCESS_KEY_ID
    AWS_SECRET_ACCESS_KEY=$AWS_SECRET_ACCESS_KEY


