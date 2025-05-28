Software stacks
==========================


Conda environments and Jupyter kernels
----------------------------------------
The Purdue Analysis Facility provides multiple Jupyter kernels with pre-installed
analysis software. Users can also create their own kernels from scratch
or from the existing kernels using the following instructions:
:doc:`guide-conda`.

.. .. image:: images/kernels.png
..    :width: 300
..    :align: center

The pre-installed kernels are listed below. The versions of the packages
in these kernels will be occasionally upgraded.

1. Python3 kernel (default)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

This kernel is based on Python 3.10 and designed for typical pythonic HEP analysis
workflows.

* In new Jupyter notebooks, this kernel will be automatically selected as default.
* In Terminal, it can be activated as follows:

  .. code-block:: shell

    conda activate /depot/cms/kernels/python3

The environment is built from the following YAML file:

.. raw:: html

   <details>
   <summary>python3-env.yaml</summary>

.. literalinclude:: python3-env.yaml
   :language: yaml

.. raw:: html

   </details>

2. ``coffea_latest`` kernel
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

This kernel features the latest version of `Coffea <https://coffeateam.github.io/coffea/>`_ package,
which is regularly updated. In contrtast, the Coffea version in the default
environment is fixed to ``0.7.21``.

.. note::

   If you want to use the ``coffea_latest`` environment but missing some packages,
   please :doc:`contact Purdue AF admins <doc-support>` and we will install them
   for you.

.. note::

   - Last updated: May 28, 2025
   - Coffea version: ``2025.3.0``

* In new Jupyter notebooks, this kernel will appear as ``Python[conda env:coffea_latest]``.
* In Terminal, it can be activated as follows:

  .. code-block:: shell

    conda activate /depot/cms/kernels/coffea_latest

The environment is built from the following YAML file:

.. raw:: html

   <details>
   <summary>coffea_latest.yaml</summary>

.. literalinclude:: coffea_latest.yaml
   :language: yaml

.. raw:: html

   </details>

3. ROOT C++ kernel
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

This kernel provides an interactive interface to the ROOT command line,
allowing to execute ROOT macros and produce plots inside Jupyter notebooks.

.. seealso::

   :doc:`ROOT C++ notebook demo <demos/root-cpp>`



Software stacks from CVMFS
--------------------------------

It is possible to load centrally distributed CERN software stacks for CVMFS, such as
LCG Releases and Apptainer/Singularity images. 

.. warning::

   Methods described in this section will work only in Terminal, it is not currently
   possible to use the LCG stacks and Apptainer images to launch custom Jupyter kernels.


1. LCG Releases
~~~~~~~~~~~~~~~~~~~

The `LCG Documentation <https://lcgdocs.web.cern.ch/lcgdocs/lcgreleases/introduction/>`_ page
contains information about software releases avaiable for loading via CVMFS.
Loading remote software is possible either via LCG **views** for entire software stacks,
or via LCG **releases** for specific packages.



2. Apptainer (Singularity) images
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Another method of loading centrally maintained software is via Apptainer (formerly Singularity)
images distributed via CVMFS. This can be useful if you need to run a code that requires a 
specific operating system (Purdue AF is based on AlmaLinux8).

Example of loading an Apptainer image based on EL7:

.. code-block::

   $ /cvmfs/cms.cern.ch/common/cmssw-el7
   Singularity> 

.. warning::

   Your Analysis Facility session already runs in a Docker container. Launching Apptainer
   inside the AF session leads to a "container-in-container" setup, which is not guaranteed
   to always work as intended.

   We do not recommend using Apptainer at Purdue AF, unless it is absolutely needed.