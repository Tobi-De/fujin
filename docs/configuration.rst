Configuration
=============

.. automodule:: fujin.config


Example
-------

This is a minimal working example.

.. tab-set::

    .. tab-item:: python package

        .. exec_code::
            :language_output: toml

            # --- hide: start ---
            from fujin.commands.init import simple_config
            from tomli_w import dumps

            print(dumps(simple_config("bookstore"),  multiline_strings=True))
            #hide:toggle

    .. tab-item:: binary mode

        .. exec_code::
            :language_output: toml

            # --- hide: start ---
            from fujin.commands.init import binary_config
            from tomli_w import dumps

            print(dumps(binary_config("bookstore"),  multiline_strings=True))
            #hide:toggle
