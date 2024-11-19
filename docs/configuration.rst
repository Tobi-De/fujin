Configuration
=============

.. automodule:: fujin.config


Example
-------

This is a minimal working example.

.. tab-set::

    .. tab-item:: python package

        .. jupyter-execute::
            :hide-code:

            from fujin.commands.init import simple_config
            from tomli_w import dumps

            print(dumps(simple_config("bookstore")))

    .. tab-item:: binary mode

        .. jupyter-execute::
            :hide-code:

            from fujin.commands.init import binary_config
            from tomli_w import dumps

            print(dumps(binary_config("bookstore")))
