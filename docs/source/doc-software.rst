Software
==========================

The Purdue Analysis Facility provides two Jupyter kernels with pre-installed
analysis software. Users can also create their own kernels from scratch
or from the existing kernels using the following instructions:
:doc:`guide-conda`.

The pre-installed kernels are listed below. The versions of the packages
in these kernels will be occasionally upgraded.

.. image:: images/kernels.png
   :width: 300
   :align: center

**Python3 kernel (default)**

This kernel is based on Python 3.10 and designed for typical pythonic HEP analysis
workflows. The kernel corresponds to the conda environment located
at ``/depot/cms/kernels/python3``, which is built from the following YAML file:

.. literalinclude :: python3-env.yaml
   :language: yaml

**ROOT C++ kernel**

This kernel provides an interactive interface to the ROOT command line,
allowing to execute ROOT macros and produce plots inside Jupyter notebooks.

   :doc:`ROOT C++ notebook demo <demos/root-cpp>`