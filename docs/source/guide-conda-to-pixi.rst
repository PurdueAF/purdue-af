Conda to Pixi migration guide
====================================================

The Purdue Analysis Facility is migrating from Conda/Mamba to Pixi for environment
management. Pixi is significantly faster than Conda and addresses multiple issues
we have experienced with Conda. This guide will help you to start using Pixi in you
projects.

Please read this guide carefully, and try to create Pixi environments in your projects.
Once you are confortable with the basec setup, you can explore more advanced Pixi features
at the official `Pixi documentation website <https://pixi.sh>`_.


Why migrate to Pixi?
~~~~~~~~~~~~~~~~~~~~~

Pixi offers several advantages over Conda:

* **Much faster**: Package installation and environment resolution is significantly
  faster than Conda/Mamba. For instance, building a Conda environment with all common
  HEP analysis packages and ML libraries only takes about a minute.
* **Better dependency management**: Pixi uses a more comprehensive dependency resolution
  across both Conda and PyPI packages (Conda, on the other hand, does not cross-check
  PyPI and Conda dependencies, which may lead to conflicts).
* **Better reproducibility**:

  * Configuration files (``pixi.toml``) are always used to define environments;
    they are automatically updated when a package is manually installed on top of an
    existing environment.
    In Conda, manual installation of a package would lead to discrepancy between the
    environment itself and the ``environment.yaml`` file that describes it.
  * Lock files ensure exact package versions across different systems.

* **More robust**: Conda is very sensitive to environment variables and easily breaks
  system paths; Pixi is not as fragile.

Key differences from Conda
~~~~~~~~~~~~~~~~~~~~~~~~~~~

1. **Project-based environments**: Pixi environments are meant to be project-specific rather
   than global. With Conda, we used to create environments in arbitrary directories and 
   "activate" them remotely (e.g. using ``conda activate /path/to/my-env``). With Pixi,
   environments are co-located with analysis code, and all Pixi commands executed in the project
   directory are executed in the context of the project-specific environment.
   
   This may sound like sharing the exact same environment between different projects is difficult.
   However, a Pixi environment is fully defined by the ``pixi.toml`` file, therefore sharing
   an environment is as easy as copying the ``pixi.toml`` file to the new project.
   The only downside of this approach is duplication of built packages, but we believe that
   Pixi's advantages well outweigh this. Moreover, different environments will share build cache,
   so it will be extremely fast to install a package into a new environment if you already have
   it in another environment.

   .. note::

      You can still "activate" a Pixi environment in a project directory by running ``pixi shell``,
      and then switch to another directory and continue usin the activated environment.

   .. note::

      We do provide one global environment at ``/work/pixi/global/``, which includes most of the
      common HEP packages and ML libraries. This environment can be used as a starting point to
      run code and notebooks that are not part of a Pixi project.

2. **Package installation**: in both Conda and Pixi, you can build an enviuronment from a
   configuration file - ``environment.yaml`` or ``pixi.toml``, respectively. However, in Conda,
   if you then manually install a package via ``conda install`` or ``mamba install``, the environment
   will no longer be synchronized with the configuration file and therefore much harder to reproduce.
   
   Pixi, on the other hand, enforces reproducibility by design: if you manually install a package
   via ``pixi add``, the ``pixi.toml`` file will be automatically updated to reflect the new package.

   Additionally, Pixi will create a lock file (``pixi.lock``) that will always ensure that the
   environment is synchronized with the ``pixi.toml`` file.


3. **Jupyter kernels**: Jupyter kernels reated from Conda environemtns are installed automatically
   by scanning Conda paths for valid environments. This is not possible in Pixi, as Pixi does not
   maintain a global environment registry. Instead, we provide two special Pixi kernels - "global"
   and "project-aware" - see details below in the Jupyter kernels section.
   

4. **Dask Gateway**: at the moment, there are no fundamental differences between the use of
   Conda and Pixi in Dask Gateway, because Pixi environments are structured similarly to Conda
   environments. To use Pixi environments in Dask Gateway, we have added a couple of new parameters
   to the ``new_cluster()`` function - see more details below in the Dask Gateway section.


Storage locations
~~~~~~~~~~~~~~~~~

Pixi-based projects must be located outside of ``/home/`` to avoid ``/home/`` storage overflow.

You can use the following locations:

* **Purdue users**:

  * ``/depot/cms/users/<username>/``
  * ``/depot/cms/<group-name>/``
  * ``/work/users/<username>/``
  * ``/work/projects/<project-name>/``

* **Non-Purdue users** (CERN/FNAL):

  * ``/work/users/<username>/``
  * ``/work/projects/<project-name>/``

.. warning::

   Attempting to run ``pixi shell`` or ``pixi install`` in ``/home/`` will result in an error.


Quickstart
~~~~~~~~~~

To get started with Pixi, you can either create a new Pixi environment from scratch,
or convert an existing Conda environment to Pixi. We recommend the first option, so that
you end up with a cleaner and smaller environment with only the packages you need.

Option A: Create a new Pixi environment from scratch
-----------------------------------------------------

**Step 1: initialize a new Pixi project**

.. code-block:: shell

   cd /your/project/directory

   pixi init

This will create a new ``pixi.toml`` file in the project directory which looks like this:

.. code-block:: toml

   [workspace]
   authors = ["Your Name <your.email@example.com>"]
   channels = ["conda-forge"]
   name = "project-name"
   platforms = ["linux-64"]
   version = "0.1.0"

   [tasks]

   [dependencies]

The ``[dependencies]`` section is where you can add packages to the environment;
the ``[tasks]`` section allows you to define custom commands that can be executed in the
context of the environment. To add ``pip`` packages, you can add a ``[pypi-dependencies]``
section and list the packages there.

**Step 2: add packages to the environment**

.. code-block:: shell

   # add Conda packages via command line:
   pixi add coffea==2025.12.0
   #... or edit the [dependencies] section of the pixi.toml file

   # add PyPI packages via command line:
   pixi add --pypi cmsstyle
   #... or edit the [pypi-dependencies] section of the pixi.toml file

**Step 3: build and activate the new environment**

.. code-block:: shell

   # build (if not built yet) and activate the environment
   pixi shell

   # OR, to build only:
   pixi install



Option B: Convert an existing Conda environment to Pixi
-------------------------------------------------------

You can convert an existing Conda environment if you have an existing ``environment.yaml`` file.

.. code-block:: shell

   cd /your/project/directory

   # this will create pixi.toml file with all dependencies from the Conda environment.yaml file
   pixi init --import /path/to/environment.yaml

   # build and activate the environment
   pixi shell

   # NOTE: Pixi may find conflicts between Conda and PyPI packages that you didn't know existed!


Pixi kernels in Jupyter
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Instead of one-to-one mapping between environments and Jupyter kernels, we provide two
special Pixi kernels:

- "global" kernel: always uses the global environment at ``/work/pixi/global/``. This environment
  is built with all the common HEP packages and ML libraries and can be used as a starting point
  for new projects.
- "project-aware" kernel: automatically discovers the environment local to the directory
  where the notebook is located. If no local environment is found, the kernel will use the global
  environment.

  .. note::

     In order for a Pixi environment to be discoverable by the "project-aware" kernel, it must
     have the ``ipykernel`` package installed.


Dask Gateway
~~~~~~~~~~~~

To use Pixi environments in Dask Gateway, simply pass the argument ``pixi_project`` to the
``new_cluster()`` method (it can't be used together with ``conda_env`` argument).
This argument must point to the directory that contains the ``pixi.toml`` file.

Additionally, you can specify ``pixi_env`` argument (``dafault`` if not specified),
if your Pixi project is using a `multi-environment setup <https://pixi.sh/dev/workspace/multi_environment/>`_.

As with Conda environments, the Pixi environment location must be visible to Dask workers,
which means ``/depot/*/`` if you are using Dask Gateway with Slurm backend;
``/depot/*/`` or ``/work/*/`` if you are using Dask Gateway with Kubernetes backend.


Combine in Pixi environments
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

You can easily install Combine in your own Pixi environment by adding a "task"
(custom command) to your Pixi project. All you nned to do ia to copy the following
code block to the ``[tasks]`` section of the ``pixi.toml`` file:

.. code-block:: toml

   [tasks]
   install_combine = """
   sh -c '
   set -e

   # Delete existing Combine installation, if present
   rm -rf HiggsAnalysis/CombinedLimit

   # Clone latest Combine version from GitHub
   git clone https://github.com/cms-analysis/HiggsAnalysis-CombinedLimit.git HiggsAnalysis/CombinedLimit
   cd HiggsAnalysis/CombinedLimit

   # Get the Python major.minor version from the active pixi env
   PY_VER=$(python - << \"PY\"
   import sys
   print(f\"{sys.version_info.major}.{sys.version_info.minor}\")
   PY
   )

   # Install Combine into the environment
   cmake -S . -B build \
   -DCMAKE_INSTALL_PREFIX=\"$CONDA_PREFIX\" \
   -DCMAKE_INSTALL_PYTHONDIR=\"lib/python${PY_VER}/site-packages\" \
   -DUSE_VDT=OFF
   cmake --build build -j\"$(nproc --ignore=2)\"
   cmake --install build

   cd -
   '
   """

Then, once your environment is built, you can install Combine by running the following
command:

.. code-block:: shell

   pixi run install_combine

Once installed, you can use Combine commands in your project.