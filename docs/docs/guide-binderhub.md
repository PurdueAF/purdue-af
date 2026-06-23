# Sharing reproducible code via BinderHub

[BinderHub](https://binderhub.readthedocs.io/en/latest/) is an open-source service
that allows you to turn a Git repository into a collection of interactive Jupyter
notebooks, which can then be shared and executed outside of Purdue AF without
needing an account or installing any software.

This can be useful for sharing your code with people who do not have access to
Purdue AF, for example to host public workshops or tutorials.

## How to share your code using BinderHub

Below is a step-by-step guide to export your analysis code from your private
Purdue AF instance and make it executable via BinderHub.

1. **Save your code to a GitHub repository**

    Ensure that all your analysis code, notebooks, and any other necessary files
    are saved in a public GitHub or GitLab repository. This repository will be the
    basis for BinderHub to recreate your working environment.

    To turn an existing directory into a Git repository, you can use the
    [git init](https://github.com/git-guides/git-init) command.

2. **Create an `environment.yml` file**

    In order for BinderHub to create the necessary environment for your analysis,
    you need to define the dependencies in an `environment.yml` file, which
    specifies the Python packages and versions needed to run the code.

    This file should be placed in the root directory of your repository.

    Example `environment.yml`:

    ```yaml
    name: my-env
    channels:
    - defaults
    - conda-forge
    dependencies:
    - python=3.8
    - numpy
    - matplotlib
    - pandas
    - coffea=0.7.21
    ```

    There are several ways you can define `environment.yml`:

    1. List the packages manually, as in the example above.
    2. Export the full list of packages and dependencies from an existing Conda
       environment:

        ```shell
        conda activate /path/to/environment
        conda env export --no-builds | grep -v "^prefix: " > environment.yml
        ```

        In the command above we omit the environment's "prefix", as it specifies
        the directory in which the Conda environment is installed — this is not
        needed for BinderHub.

    !!! note

        BinderHub expects a Conda-style `environment.yml`, even if your project
        uses Pixi. If your project is Pixi-based, list the same dependencies from
        your `pixi.toml` in an `environment.yml` file.

3. **Launch a BinderHub session**

    Once a Git repository with your code and `environment.yml` is ready, it can be
    launched at a public BinderHub instance as follows:

    1. Navigate to a public BinderHub instance, e.g. [mybinder.org](https://mybinder.org).
    2. Paste the link to your Git repository in the window labeled
       *"GitHub repository name or URL"*.
    3. Press `Launch` and wait for BinderHub to build your environment and launch
       JupyterHub.

    !!! important

        Be aware of the resource limits imposed at mybinder.org — sessions
        exceeding these limits will be killed:

        * no more than 100 users running the same repository simultaneously;
        * maximum 2 GB memory per user session.
