# Managing environments with Pixi

The Purdue Analysis Facility is migrating from Conda/Mamba to **Pixi** for
environment management. Pixi is significantly faster than Conda and addresses
multiple issues we have experienced with Conda over the years. This guide will help
you start using Pixi in your projects.

Please read this guide carefully and try to create Pixi environments in your
projects. Once you are comfortable with the basic setup, you can explore more
advanced Pixi features at the official
[Pixi documentation website](https://pixi.sh).

## Why migrate to Pixi?

Pixi offers several advantages over Conda:

* **Much faster**: package installation and environment resolution is significantly
  faster than with Conda/Mamba. For instance, building a Pixi environment with all
  common HEP analysis packages and ML libraries only takes about a minute.
* **Better dependency management**: Pixi performs comprehensive dependency
  resolution across both Conda and PyPI packages. Conda, on the other hand, does
  not cross-check PyPI and Conda dependencies, which may lead to conflicts.
* **Better reproducibility**:

    * Configuration files (`pixi.toml`) are always used to define environments,
      and they are automatically updated when a package is installed on top of an
      existing environment. In Conda, manual installation of a package leads to a
      discrepancy between the environment itself and the `environment.yaml` file
      that supposedly describes it.
    * Lock files (`pixi.lock`) ensure exact package versions across different systems.

* **More robust**: Conda is very sensitive to environment variables and easily
  breaks system paths; Pixi is not as fragile.

## Key differences from Conda

1. **Project-based environments.** Pixi environments are meant to be
   project-specific rather than global. With Conda, we used to create environments
   in arbitrary directories and "activate" them remotely (e.g. using
   `conda activate /path/to/my-env`). With Pixi, environments are co-located with
   the analysis code, and all Pixi commands executed in the project directory are
   executed in the context of the project-specific environment.

    This may sound like sharing the exact same environment between different
    projects is difficult. However, a Pixi environment is fully defined by the
    `pixi.toml` file, therefore sharing an environment is as easy as copying the
    `pixi.toml` file to the new project. The only downside of this approach is
    duplication of built packages, but we believe that Pixi's advantages well
    outweigh this. Moreover, different environments share a build cache, so
    installing a package into a new environment is extremely fast if you already
    have it in another environment.

    !!! note

        You can still "activate" a Pixi environment in a project directory by
        running `pixi shell`, and then switch to another directory and continue
        using the activated environment.

    !!! note

        We also provide one global environment at `/work/pixi/global/`, which
        includes most of the common HEP packages and ML libraries. This environment
        can be used as a starting point to run code and notebooks that are not part
        of a Pixi project.

2. **Package installation.** In both Conda and Pixi, you can build an environment
   from a configuration file — `environment.yaml` or `pixi.toml`, respectively.
   However, in Conda, if you then manually install a package via `conda install` or
   `mamba install`, the environment is no longer synchronized with the configuration
   file and therefore much harder to reproduce.

    Pixi, on the other hand, enforces reproducibility by design: if you install a
    package via `pixi add`, the `pixi.toml` file is automatically updated to
    reflect the new package. Additionally, Pixi creates a lock file (`pixi.lock`)
    that always keeps the environment synchronized with the `pixi.toml` file.

3. **Jupyter kernels.** Jupyter kernels created from Conda environments are
   installed automatically by scanning Conda paths for valid environments. This is
   not possible in Pixi, as Pixi does not maintain a global environment registry.
   Instead, we provide two special Pixi kernels — "global" and "project-aware" —
   see the [Pixi kernels in Jupyter](#pixi-kernels-in-jupyter) section below.

4. **Dask Gateway.** At the moment, there are no fundamental differences between
   the use of Conda and Pixi in Dask Gateway, because Pixi environments are
   structured similarly to Conda environments. To use Pixi environments in Dask
   Gateway, we have added a couple of new parameters to the `new_cluster()`
   function — see the [Dask Gateway](#pixi-environments-in-dask-gateway) section below.

## Storage locations

Pixi-based projects must be located **outside of `/home/`** to avoid overflowing
your home storage quota.

You can use the following locations:

* **Purdue users:**

    * `/depot/cms/users/<username>/`
    * `/depot/cms/<group-name>/`
    * `/work/users/<username>/`
    * `/work/projects/<project-name>/`

* **Non-Purdue users (CERN/FNAL):**

    * `/work/users/<username>/`
    * `/work/projects/<project-name>/`

!!! warning

    Attempting to run `pixi shell` or `pixi install` in `/home/` will result in
    an error.

!!! note "Dask Gateway with the Slurm backend"

    Slurm workers can only see `/depot/` storage. If you plan to use your
    environment with the Slurm backend of Dask Gateway, keep the project on Depot —
    see [Storage volumes](storage.md).

## Quickstart

To get started with Pixi, you can either create a new Pixi environment from
scratch, or convert an existing Conda environment to Pixi. We recommend the first
option, so that you end up with a cleaner and smaller environment containing only
the packages you need.

### Option A: Create a new Pixi environment from scratch

**Step 1: initialize a new Pixi project**

```shell
cd /your/project/directory

pixi init
```

This creates a new `pixi.toml` file in the project directory, which looks like this:

```toml
[workspace]
authors = ["Your Name <your.email@example.com>"]
channels = ["conda-forge"]
name = "project-name"
platforms = ["linux-64"]
version = "0.1.0"

[tasks]

[dependencies]
```

The `[dependencies]` section is where you add packages to the environment;
the `[tasks]` section allows you to define custom commands that can be executed in
the context of the environment. To add `pip` packages, add a `[pypi-dependencies]`
section and list the packages there.

**Step 2: add packages to the environment**

```shell
# add Conda packages via command line:
pixi add coffea==2025.12.0
#... or edit the [dependencies] section of the pixi.toml file

# add PyPI packages via command line:
pixi add --pypi cmsstyle
#... or edit the [pypi-dependencies] section of the pixi.toml file
```

**Step 3: build and activate the new environment**

```shell
# build (if not built yet) and activate the environment
pixi shell

# OR, to build only:
pixi install
```

!!! tip

    If you want the environment to be usable as a Jupyter kernel, don't forget to
    add `ipykernel` to the dependencies:

    ```shell
    pixi add ipykernel
    ```

### Option B: Convert an existing Conda environment to Pixi

You can convert an existing Conda environment if you have its `environment.yaml` file:

```shell
cd /your/project/directory

# this will create a pixi.toml file with all dependencies
# from the Conda environment.yaml file
pixi init --import /path/to/environment.yaml

# build and activate the environment
pixi shell
```

!!! note

    Pixi may find conflicts between Conda and PyPI packages that you didn't know
    existed! This is expected — Conda never checked for them.

## Pixi kernels in Jupyter

Instead of a one-to-one mapping between environments and Jupyter kernels, we
provide two special Pixi kernels:

* **"global" kernel** — always uses the global environment at `/work/pixi/global/`.
  This environment is built with all the common HEP packages and ML libraries and
  can be used as a starting point for new projects.
* **"project-aware" kernel** — automatically discovers the environment local to the
  directory where the notebook is located. If no local environment is found, the
  kernel uses the global environment.

!!! note

    In order for a Pixi environment to be discoverable by the "project-aware"
    kernel, it must have the `ipykernel` package installed.

## Pixi environments in Dask Gateway

To use a Pixi environment in Dask Gateway, simply pass the `pixi_project` argument
to the `new_cluster()` method (it can't be used together with the `conda_env`
argument). This argument must point to the directory that contains the `pixi.toml`
file:

```python
cluster = gateway.new_cluster(
    pixi_project = "/path/to/pixi/project",
    # ...
)
```

Additionally, you can specify the `pixi_env` argument (`default` if not specified)
if your Pixi project uses a
[multi-environment setup](https://pixi.sh/dev/workspace/multi_environment/).

As with Conda environments, the Pixi environment location must be visible to the
Dask workers: `/depot/*/` if you are using Dask Gateway with the Slurm backend;
`/depot/*/` or `/work/*/` if you are using Dask Gateway with the Kubernetes backend.
See [Dask Gateway at Purdue AF](guide-dask-gateway.md) for details.

## Combine in Pixi environments

You can easily install Combine into your own Pixi environment by adding the
`cms-combine` package as a dependency, either by running

```shell
pixi add cms-combine==10.4.2
```

in your project directory, or by adding it explicitly to the `[dependencies]`
section of the `pixi.toml` file:

```toml
[dependencies]
cms-combine = "==10.4.2"
```

You can also install Combine from source — see
[Using Combine at Purdue AF](guide-combine.md) for more details.
