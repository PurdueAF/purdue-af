How to create Conda environments and Jupyter kernels
====================================================

In the Purdue Analysis Facility, the Python-based Jupyter kernels are created from conda environments.
Two pre-installed environments named `python3` and `python3-ml` are stored in `/depot/cms/kernels/`,
the packages installed in these environments are listed :doc:`here <doc-storage>`.

List all available conda environments: ``conda env list``

List all available kernels: ``jupyter kernelspec list``

Minimal example of creating a custom kernel
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The basic recipe to create a custom kernel is straightforward:

#. Create a conda environment in a desired location with a desired name.
#. Istall ``ipykernel`` package and wait for 1-2 minutes.
#. A new kernel with the same name as the conda environment will appear in Jupyter.


.. code-block:: shell
    
    # path to your conda environments on Depot:
    conda_envs_path="/depot/cms/conda_envs/$USER"
    
    # name of the new environment:
    conda_env_name="my-new-env"
    
    # create a new environment
    conda create -y --prefix $conda_envs_path/$conda_env_name python=3.10 ipykernel
    
    # activate environment
    conda activate $conda_envs_path/$conda_env_name
    
.. warning::
    Since the kernel names are based on the conda environment names,
    one should avoid creating multiple conda environments with the same name.
    Also, one should avoid using names ``python3`` and ``python3-ml`` to name conda environments,
    as these names are reserved for the pre-installed kernels.


Creating custom conda environments and Jupyter kernels
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

There are multiple ways to create a custom conda environment,
the particular choice of a method depends on the use case.


Option 1: Create a conda environment from scratch
--------------------------------------------------

This option is preferred if you want to start from a clean environment and install all packages manually.

.. code-block:: shell
    conda create --prefix /some-path/my-new-env python=3.10 ipykernel
    conda activate /some-path/my-new-env
    conda install numpy pandas # install any packages here
    conda deactivate

Option 2: Clone an existing environment into a new environment
----------------------------------------------------------------

This is a simple method to duplicate an existing environment. 

.. code-block:: shell
    conda create --prefix /path/to/cloned_env --clone /path/to/original_env

Option 3: Create a conda environment from a YAML file
----------------------------------------------------------------

This is another method to replicate an environment, it can be used if the original environment
is exported and shared as a YAML file.
The main benefit of this approach is the possibility to share environments outside of the Analysis Facility
(one can simply email the YAML file).

Alternatively, this method can be used to create a conda environment from scratch,
if you know in advance which packages must be present in the kernel.

#. If you have already been provided with a YAML file, proceed to step 5.
#. If you are creating a YAML file from scratch, you can use one of the files made
for the pre-installed kernels as a template:
``/depot/cms/kernels/env-python3.yml``
``/depot/cms/kernels/env-python3-ml.yml``
#. Copy one of these files to your preferred location.
    
.. code-block:: shell
    cp /depot/cms/kernels/env-python3.yml /some-path/my-env-file.yml
    
#. Edit the contents of the copied YAML file by adding/removing packages and/or specifying their versions.
#. Additional Conda repositories may be specified under the channels: section,
as it is done in ``/depot/cms/kernels/env-conda-analysis-ml.yml``:

.. code-block:: yaml
    channels:
      - conda-forge
      - pyg

#. Once the list of packages is finalized, create a conda environment in a desired location
(in this example the environment will get created with a name ``my-new-env``):

.. code-block:: shell
    conda env create -f /some-path/my-env-file.yml --prefix /some-path/my-new-env

.. warning::
    Keep in mind that conda environments can take up a lot of space (up to several dozen GB),
    so the ``/home/<username>/`` storage space may be insufficient for storing more than 1-2 custom environments.
    Purdue users can store their environments in Depot storage.

#. You can activate the environment and install more packages into it at any time.



Uninstalling a conda environment
---------------------------------

.. code-block:: shell
    # list available environments
    conda info --envs

    # uninstall an environment by name or by path
    conda remove --name <env-name> --all
    # or
    conda remove --prefix /path/to/env --all