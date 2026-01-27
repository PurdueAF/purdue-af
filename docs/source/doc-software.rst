Software stacks
==========================


Pixi enviromnments
--------------------------------

The main way to manage analysis software starting from AF release 0.12.0 is via Pixi enviromnments.
`Pixi <https://pixi.sh/>`_ is a modern package manager which is a successor of Conda/Mamba.

Unlike Conda environments, Pixi enviromnments are meant to be project-specific and are
stored in the project directory. A detailed guide on how to start using Pixi is available
:doc:`here <guide-conda-to-pixi>`.

In addition to project-specific Pixi environments, we provide a global Pixi environment,
which contains all common HEP analysis packages and ML libraries; the environment is
located at ``/work/pixi/global/``.

.. admonition:: pixi.toml config for the global environment
   :class: toggle

   .. literalinclude:: pixi-global.toml
      :language: toml


Jupyter kernels
---------------

We provide multiple types of Jupyter kernels to execute analysis code in notebooks.

Pixi kernels
~~~~~~~~~~~~

There is no one-to-one mapping between Pixi environments and Jupyter kernels.
Instead, we provide two special Pixi kernels - "global" and "project-aware"

- "global" kernel: always uses the global environment at ``/work/pixi/global/``. This environment
  is built with all the common HEP packages and ML libraries and can be used as a starting point
  for new projects.
- "project-aware" kernel: automatically discovers the environment local to the directory
  where the notebook is located. If no local environment is found, the kernel will use the global
  environment.

  .. note::

     In order for a Pixi environment to be discoverable by the "project-aware" kernel, it must
     have the ``ipykernel`` package installed.

Conda kernels
~~~~~~~~~~~~~

Conda environments can be automatically discovered by Jupyter if they have the
``ipykernel`` package installed.

.. warning::

   Automatic environment discovery means that Jupyter always scans directories where
   environments are stored. If one of this directories is on a slow filesystem, it may
   significantly slow down the entire AF session.

   Because of this, automatic Conda environment discovery will be removed in the future,
   although it will still be possible to add kernels manually.

ROOT C++ kernel
~~~~~~~~~~~~~~~

This kernel provides an interactive interface to the ROOT command line,
allowing to execute ROOT macros and produce plots inside Jupyter notebooks.

.. seealso::

   :doc:`ROOT C++ notebook demo <demos/root-cpp>`


LCG_106b_cuda kernel
~~~~~~~~~~~~~~~~~~~~~

This kernel is based on the LCG "view" ``LCG_106b_cuda`` loaded via CVMFS.
It contains the CUDA-enabled ROOT build and is suitable for running RooFit on GPUs.


Combine
--------------------------------

`Combine <https://cms-analysis.github.io/HiggsAnalysis-CombinedLimit/v10.3.3/>`_ is
a RooStats / RooFit based software framework widely used in LHC experiments for
statistical analysis of experimental data.

At Purdue AF, Combine can be used either in a CMSSW environment or in standalone mode
in Pixi or Conda environments.

See :doc:`guide-combine` for more details.


Apptainer / Singularity images
------------------------------

In rare cases when you need to run a code that requires a specific operating system
(Purdue AF is based on AlmaLinux8), you can load Apptainer/Singularity images via CVMFS.

Example of loading an Apptainer image based on EL7:

.. code-block::

   $ /cvmfs/cms.cern.ch/common/cmssw-el7
   Singularity> 

.. warning::

   Your Analysis Facility session already runs in a Docker container. Launching Apptainer
   inside the AF session leads to a "container-in-container" setup, which is not guaranteed
   to always work as intended.

   We do not recommend using Apptainer at Purdue AF, unless it is absolutely needed.