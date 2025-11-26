Using Combine at Purdue AF
================================

`Combine <https://cms-analysis.github.io/HiggsAnalysis-CombinedLimit/v10.3.3/>`_ is
a RooStats / RooFit based software framework widely used in LHC experiments for
statistical analysis of experimental data.

Combine can be used either in a CMSSW environment or in standalone mode.
At Purdue AF, we recommend using **standalone mode** because some CMSSW releases
are not compatible with the operating system, and loading other operating systems
 — while possible via Singularity — can cause unexpected issues.

Combine in pre-installed environments
--------------------------------------

Standalone Combine is pre-installed in the two centrally managed Conda environments
- ``/depot/cms/kernels/python3`` and ``/depot/cms/kernels/coffea_latest``; it is enough to
it is enough to activate either of these environments to use Combine.

Combine in custom environments
------------------------------

If you want to use Combine in your own environment, you can use the following instructions:

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

Troubleshooting
----------------

If you encounter errors while trying to use Combine, you can try the following:

- Print out ``PATH``, ``LD_LIBRARY_PATH`` and ``PYTHONPATH`` environment variables.
  They should contain the paths to the Combine build directory
  (e.g. ``/depot/cms/purdue-af/combine/HiggsAnalysis/CombinedLimit/build/`` for centrally managed environments).

- For custom environments:

  - Make sure that your environment is activated and contains all Combine dependencies listed above;
  - Re-run the installation commands and make sure that ``$CONDA_PREFIX`` points to the
    environment where you want Combine installed.
