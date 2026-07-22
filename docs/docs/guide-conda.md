# Creating Conda environments and Jupyter kernels

!!! warning "Conda is being phased out"

    The recommended way to manage analysis software at Purdue AF is now
    [Pixi](guide-pixi.md). Conda environments are still fully supported, but
    automatic Conda kernel discovery will be removed in the future. For new
    projects, please use Pixi.

In the Purdue Analysis Facility, Python-based Jupyter kernels can be created from
Conda environments.

* List all available Conda environments:

    ```shell
    conda env list
    ```

* List all available Jupyter kernels:

    ```shell
    jupyter kernelspec list
    ```

    or simply click the `[+]` button (New Launcher) in the AF interface.

## Creating a custom Jupyter kernel: minimal example

The basic recipe to create a custom kernel is straightforward:

1. Create a Conda environment in a desired location with a desired name
   (see different ways to create Conda environments below).
2. Install the `ipykernel` package and wait for 1–2 minutes.
3. A new kernel with the same name as the Conda environment will appear in Jupyter.

```shell
# path to your Conda environments on Depot:
conda_envs_path="/depot/cms/conda_envs/$USER"

# or under /work/, if you are not a Purdue user:
# conda_envs_path="/work/users/$USER"

# name of the new environment:
conda_env_name="my-new-env"

# create a new environment with the ipykernel package installed
conda create -y --prefix $conda_envs_path/$conda_env_name python=3.12 ipykernel

# activate the environment
conda activate $conda_envs_path/$conda_env_name
```

!!! warning

    Since Jupyter kernel names are based on the Conda environment names, avoid
    creating multiple Conda environments with the same name. Also, avoid using the
    names `python3` and `coffea_latest`, as these names are reserved for
    pre-installed kernels.

## Creating custom Conda environments

There are multiple ways to create a custom Conda environment; the particular choice
of method depends on the use case.

!!! tip

    Use `mamba` instead of `conda` where possible — it significantly accelerates
    the installation of packages.

### Option 1 (recommended): create a Conda environment from a YAML file

The main benefits of this approach are the reproducibility and portability of the
resulting environment — it can be easily rebuilt anywhere from the same YAML file.

1. Here is an example of an `environment.yaml` file:

    ```yaml
    name: my-new-env
    channels:
      - defaults
      - conda-forge
    dependencies:
      - python=3.12
      - numpy
      - pandas
      - matplotlib
      - coffea=2024.9.0
      - pip
      - pip:
        - rucio-clients
    ```

2. Additional Conda repositories may be specified under the `channels:` section, e.g.:

    ```yaml
    channels:
      - conda-forge
      - pyg
    ```

3. Once the list of packages is finalized, create a Conda environment in a desired
   location (in this example the environment will be created with the name
   `my-new-env`):

    ```shell
    mamba env create --file /some-path/environment.yaml --prefix /some-path/my-new-env
    ```

    !!! warning

        Keep in mind that Conda environments can take up a lot of space (up to
        several dozen GB), so the `/home/<username>/` storage space may be
        insufficient for storing more than 1–2 custom environments.

        A better location for your environments is either `/work/` or `/depot/`
        storage (Depot is only writable by Purdue users) —
        see [Storage volumes](storage.md).

4. To install more packages into the environment or change package versions, the
   recommended method is to add the package name and/or version into the same YAML
   file, and then update the environment using the following commands:

    ```shell
    conda activate /some-path/my-new-env
    mamba env update --file /path/to/environment.yaml
    ```

### Option 2: create a Conda environment from scratch

This option is preferred if you want to start from a clean environment and install
all packages manually.

```shell
conda create --prefix /some-path/my-new-env python=3.12 ipykernel
conda activate /some-path/my-new-env
conda install numpy pandas # install any packages here
conda deactivate
```

### Option 3: clone an existing environment into a new environment

This is a simple method to duplicate an existing environment:

```shell
conda create --prefix /path/to/cloned_env --clone /path/to/original_env
```

### Uninstalling a Conda environment

```shell
# list available environments
conda info --envs

# uninstall an environment by name or by path
conda remove --name <env-name> --all
# or
conda remove --prefix /path/to/env --all
```
