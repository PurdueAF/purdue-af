Sharing reproducible code via BinderHub
=========================================

BinderHub is an open-source service that allows you to turn a Git repository
into a collection of interactive Jupyter notebooks, which can then be shared
and executed outside of Purdue AF without needing to install any software.

This can be useful for sharing your code with people who do not have access to
Purdue AF, for example to host public workshops or tutorials.


Steps to Export Your Analysis Code via BinderHub
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Below is a step-by-step guide to export your analysis code from your private
JupyterHub instance and make it executable via BinderHub.

1. **Save Your Code to a GitHub Repository**

   Ensure that all your analysis code, notebooks, and any other necessary
   files are saved in a public GitHub repository.
   This repository will be the basis for BinderHub to recreate your working
   environment.

2. **Create an ``environment.yml`` File**

   In order for BinderHub to create the necessary environment for your analysis,
   you need to define the dependencies in an ``environment.yml`` file.
   This file specifies the Python packages and versions needed to run the code.

   Example ``environment.yml``:

   .. code-block:: yaml

      name: my-env
      channels:
      - defaults
      - conda-forge
      dependencies:
      - python=3.8
      - numpy
      - matplotlib
      - jupyterlab
      - pandas
   
   This file ensures that BinderHub will recreate the exact environment
   necessary for your analysis to run without issues.

   Example structure:

   .. code-block:: shell

      my-analysis-repo/
      ├── notebooks/
      │   ├── analysis.ipynb
      ├── scripts/
      │   ├── data_processing.py
      ├── environment.yml
      ├── README.md

3.	**Link to BinderHub**

