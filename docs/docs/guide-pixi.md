# Managing environments with Pixi

**Pixi** is the environment manager of the Purdue Analysis Facility. This guide
covers the basic setup; more advanced Pixi features are described on the
official [Pixi documentation website](https://pixi.sh).

Pixi offers several advantages over Conda:

* **Speed**: package installation and environment resolution are significantly
  faster than with Conda/Mamba.
* **Dependency management**: Pixi resolves Conda and PyPI dependencies
  together, so conflicts between them are caught at install time.
* **Reproducibility**: the environment is always defined by `pixi.toml`, which
  `pixi add` updates automatically, and the lock file (`pixi.lock`) pins exact
  package versions across systems.

## Pixi projects

Pixi environments are **project-specific**: the environment definition
(`pixi.toml`) and the environment itself live in the project directory, next to
your analysis code, and all Pixi commands executed in the project directory run
in the context of that project's environment. You can still "activate" an
environment by running `pixi shell` in the project directory, and then switch to
another directory and continue using it.

To reuse an environment in another project, copy its `pixi.toml` file to the new
project. Different environments share a build cache, so installing a package that
you already have in another environment is fast.

The facility also provides a shared [global environment](software.md#the-global-pixi-environment)
at `/work/pixi/global/`, which can be used to run code and notebooks that are
not part of a Pixi project. Pixi environments are used in Jupyter through
[two special kernels](software.md#pixi-kernels), and in Dask Gateway through the
[`pixi_project` option](guide-dask-gateway.md#pixi-or-conda-environments).

## Storage locations

Pixi project commands (such as `pixi init`, `pixi add`, `pixi install`, and
`pixi shell`) refuse to run on a project under `/home/`, which is too small for
Pixi environments. Keep your projects on `/work/` or `/depot/` — see
[Storing custom Pixi or Conda environments](storage.md#storing-custom-pixi-or-conda-environments)
for the possible locations and which workers can see them.

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
pixi add coffea
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

## Combine in Pixi environments

See [Using Combine at Purdue AF](guide-combine.md#combine-in-pixi-environments).
