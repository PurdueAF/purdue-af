Creating Conda environments and Jupyter kernels
====================================================

In the Purdue Analysis Facility, the Python-based Jupyter kernels are created from
Conda environments. We provide a pre-installed Conda environment
(``/depot/cms/kernels/python3``), which includes most of the Python packages
commonly used for HEP analyses. This environment corresponds to the
``Python3 (default)`` Jupyter kernel.

.. tip::
   
   Before creating custom environments, consult with the :doc:`Analysis Facility support <doc-support>`.
   It may be easier to install missing packages into the default environment.


* List all available Conda environments: 

  .. code-block:: shell
    
      conda env list

* List all available Jupyter kernels:

  .. code-block:: shell
        
      jupyter kernelspec list

  or simply click the ``[+]`` button (New Launcher) in the AF interface.

Creating a custom Jupyter kernel: minimal example
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The basic recipe to create a custom kernel is straightforward:

#. Create a Conda environment in a desired location with a desired name.

   (See different ways to create Conda environments below.)
#. Istall ``ipykernel`` package and wait for 1-2 minutes.
#. A new kernel with the same name as the Conda environment will appear in Jupyter.


.. code-block:: shell
    
    # path to your Conda environments on Depot:
    conda_envs_path="/depot/cms/conda_envs/$USER"

    # or under /work/, if you are not a Purdue user:
    # conda_envs_path="/work/users/$USER"
    
    # name of the new environment:
    conda_env_name="my-new-env"
    
    # create a new environment with ipykernel package installed
    conda create -y --prefix $conda_envs_path/$conda_env_name python=3.10 ipykernel
    
    # activate environment
    conda activate $conda_envs_path/$conda_env_name
    
.. warning::
    Since Jupyter kernel names are based on the Conda environment names,
    one should avoid creating multiple Conda environments with the same name.
    Also, one should avoid using names ``python3`` and ``python3-ml`` to name
    Conda environments, as these names are reserved for the pre-installed kernels.


Creating custom Conda environments
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

There are multiple ways to create a custom Conda environment,
the particular choice of a method depends on the use case.

.. tip::

   Use ``mamba`` instead of ``conda`` where possible - it will significantly accelerate installation of packages.


Option 1 (recommended): Create a Conda environment from a YAML file
----------------------------------------------------------------

The main benefits of this approach are the reproducibility and portability of
the resulting environment - it can be easily rebuilt anywhere from the same YAML
file.

1. If you have already been provided with a YAML file, proceed to step 4.
2. If you are creating a YAML file from scratch, you can use the YAML file
   corresponding to the default kernel as an example: :doc:`see here <doc-software>`.

   .. warning::

      Do not copy ``prefix: /depot/cms/kernels/python3`` from the example YAML, as
      it will lead to errors during installation.
      
      Also, you can ignore the ``variables:`` section, it is only there for correct
      installation of the ``lhapdf`` package.

3. Additional Conda repositories may be specified under the ``channels:`` section, e.g:

   .. code-block:: yaml

      channels:
        - conda-forge
        - pyg

4. Once the list of packages is finalized, create a Conda environment in a desired location
   (in this example the environment will get created with a name ``my-new-env``):

   .. code-block:: shell

       conda env create -f /some-path/my-env-file.yml --prefix /some-path/my-new-env

   .. warning::

      Keep in mind that Conda environments can take up a lot of space
      (up to several dozen GB), so the ``/home/<username>/`` storage space
      may be insufficient for storing more than 1-2 custom environments.

      A better location to store your environment is either ``/work/`` or
      ``/depot/`` storage (Depot is only writeable by Purdue users).

5. To install more packages into the environment or change package versions,
   the recommended method is to add the package name into the same YAML file,
   and then update the environment using the following commands:

   .. code-block:: shell
      
      conda activate /some-path/my-new-env
      mamba env update --file /path/to/environment.yaml

Option 2: Create a Conda environment from scratch
--------------------------------------------------

This option is preferred if you want to start from a clean environment and install all packages manually.

.. code-block:: shell

    conda create --prefix /some-path/my-new-env python=3.10 ipykernel
    conda activate /some-path/my-new-env
    conda install numpy pandas # install any packages here
    conda deactivate

Option 3: Clone an existing environment into a new environment
----------------------------------------------------------------

This is a simple method to duplicate an existing environment. 

.. code-block:: shell

    conda create --prefix /path/to/cloned_env --clone /path/to/original_env



Uninstalling a Conda environment
---------------------------------

.. code-block:: shell

    # list available environments
    conda info --envs

    # uninstall an environment by name or by path
    conda remove --name <env-name> --all
    # or
    conda remove --prefix /path/to/env --all