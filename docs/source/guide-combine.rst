Using Combine at Purdue AF
================================

`Combine <https://cms-analysis.github.io/HiggsAnalysis-CombinedLimit/v10.3.3/>`_ is
a RooStats / RooFit based software framework widely used in LHC experiments for
statistical analysis of experimental data.

Combine can be used either in a CMSSW environment or in standalone mode in Pixi or Conda environments.
At Purdue AF, we recommend using **standalone mode** because some CMSSW releases
are not compatible with the operating system, and loading other operating systems
— while possible via Singularity — can cause unexpected issues.

Combine in Pixi environments
------------------------------

Install from conda-forge (recommended)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

You can easily install Combine from a ``conda-forge`` distribution into
your own Pixi environment by adding a ``cms-combine`` package
to your Pixi project by running the following command in the project directory
(which contains the ``pixi.toml`` file):

.. code-block:: shell

   pixi add cms-combine==10.4.2

OR add the package explicitly to the ``[dependencies]`` section of the ``pixi.toml`` file:

.. code-block:: toml

   [dependencies]
   cms-combine = "==10.4.2"


Install from source
~~~~~~~~~~~~~~~~~~~

You can also install Combine from source by adding a "task"
(custom command) to your Pixi project. This could be useful of you want to test
some features in an unreleased version of Combine.

All you need to do is to copy the following
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

.. warning::
   For this to work, your environment must have the following packages installed:
   ``root=6.34``, ``gsl``, ``boost-cpp``, ``vdt``, ``eigen``, ``tbb``, ``cmake``, ``ninja``.


Combine in pre-installed Conda environments
--------------------------------------------

Standalone Combine is pre-installed in the two centrally managed Conda environments
- ``/depot/cms/kernels/python3`` and ``/depot/cms/kernels/coffea_latest``;
it is enough to activate either of these environments to use Combine:

.. code-block:: shell

   $ conda activate /depot/cms/kernels/python3
   (/depot/cms/kernels/python3) $ combine -M Significance -d datacard.txt
   <<< Combine >>> 
   <<< v10.3.3 >>>

Combine in custom Conda environments
-------------------------------------

If you want to use Combine in a Conda environment, you can similarly install it from conda-forge
by adding the ``combine`` package to the ``environment.yaml`` file.

To install Combine from source into a Conda environment, you can follow the instructions below:

.. code-block:: bash

   git clone https://github.com/cms-analysis/HiggsAnalysis-CombinedLimit.git HiggsAnalysis/CombinedLimit
   cd HiggsAnalysis/CombinedLimit

   # configure conda-forge as the preferred channel
   conda config --set channel_priority strict
   conda config --add channels conda-forge

   # create and activate the environment
   conda activate <your-conda-environment>

   # install Combine and its dependencies:
   conda install combine root=6.34 gsl boost-cpp vdt eigen tbb cmake ninja
   # NOTE: if your environment is managed using an environment.yaml file,
   # add these dependencies to it and rebuild the environment, instead of installing them here.

   # configure and build with CMake
   cmake -S . -B build -DCMAKE_INSTALL_PREFIX=$CONDA_PREFIX -DCMAKE_INSTALL_PYTHONDIR=lib/python3.12/site-packages -DUSE_VDT=OFF
   cmake --build build -j$(nproc --ignore=2)
   cmake --install build

After installation, to use Combine in a new Terminal, you will only need to activate the environment.

