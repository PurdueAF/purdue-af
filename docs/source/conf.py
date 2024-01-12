# Configuration file for the Sphinx documentation builder.

# -- Project information

project = 'Purdue Analysis Facility'
copyright = '2023, Purdue University'
author = 'Dmitry Kondratyev, Stefan Piperov'

release = '0.1'
version = '0.1.0'

# -- General configuration

extensions = [
    'sphinx.ext.duration',
    'sphinx.ext.doctest',
    'sphinx.ext.autodoc',
    'sphinx.ext.autosummary',
    'sphinx.ext.intersphinx',
    "sphinx.ext.napoleon",
    "sphinx.ext.ifconfig",
    "sphinx.ext.viewcode",
    "nbsphinx",
    "IPython.sphinxext.ipython_console_highlighting",
    "sphinx_rtd_theme",
    "sphinx_copybutton",

]

#disable notebook execution
nbsphinx_execute = 'never'
nbsphinx_prolog = """
.. raw:: html

    <style>
        :not(.admonition) > p,
        ol,
        h2 {
            margin: 24px 0 12px;
        }
    </style>
"""

# sphinx-copybutton configuration
copybutton_prompt_text = r">>> |\.\.\. |\$ "
copybutton_prompt_is_regexp = True
copybutton_here_doc_delimiter = "EOF"

intersphinx_mapping = {
    'python': ('https://docs.python.org/3/', None),
    'sphinx': ('https://www.sphinx-doc.org/en/master/', None),
}
intersphinx_disabled_domains = ['std']

templates_path = ['_templates']

# -- Options for HTML output

# html_theme = 'sphinx_rtd_theme'
html_theme = 'furo'

# -- Options for EPUB output
epub_show_urls = 'footnote'