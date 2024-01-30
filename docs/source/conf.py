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
    "sphinx_togglebutton",
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

html_static_path = ['_static']
html_css_files = ['custom.css']

html_theme = 'furo'
html_logo = "images/cms_logo.png"
html_theme_options = {
    "top_of_page_button": None,
    "footer_icons": [
        {
            "name": "GitHub",
            "url": "https://github.com/kondratyevd/purdue-af",
            "html": """
                <svg stroke="currentColor" fill="currentColor" stroke-width="0" viewBox="0 0 16 16">
                    <path fill-rule="evenodd" d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0 0 16 8c0-4.42-3.58-8-8-8z"></path>
                </svg>
            """,
            "class": "",
        },
    ],
}
html_title = "Purdue AF"
html_sidebars = {
    "**": [
        "sidebar/brand.html",
        "sidebar/search.html",
        "sidebar/scroll-start.html",
        "sidebar/navigation.html",
        # "sidebar/ethical-ads.html",
        "sidebar/scroll-end.html",
        # "sidebar/rtd-versions.html",
        "sidebar/variant-selector.html"
    ]
}

# -- Options for EPUB output
epub_show_urls = 'footnote'