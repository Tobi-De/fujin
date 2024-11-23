# Configuration file for the Sphinx documentation builder.
#
# For the full list of built-in configuration values, see the documentation:
# https://www.sphinx-doc.org/en/master/usage/configuration.html
# -- Project information -----------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#project-information

project = "fujin"
copyright = "2024, Tobi DEGNON"
author = "Tobi DEGNON"
release = "2024"

# -- General configuration ---------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#general-configuration

extensions = [
    "cappa.ext.docutils",
    "myst_parser",
    "sphinx.ext.todo",
    "sphinx.ext.autodoc",
    "sphinx_design",
    "sphinx_copybutton",
    "sphinx_exec_code",
    "sphinx_togglebutton",
]

templates_path = ["_templates"]
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]

# -- Options for HTML output -------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#options-for-html-output

html_theme = "shibuya"
html_static_path = ["_static"]

html_theme_options = {
    "mastodon_url": "https://fosstodon.org/@tobide",
    "github_url": "https://github.com/falcopackages/fujin",
    "twitter_url": "https://twitter.com/tobidegnon",
    "discussion_url": "https://github.com/falcopackages/fujin/discussions",
    "accent_color": "teal",
    "globaltoc_expand_depth": 1,
}
