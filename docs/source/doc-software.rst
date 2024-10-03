Software
==========================

The Purdue Analysis Facility provides two Jupyter kernels with pre-installed
analysis software. Users can also create their own kernels from scratch
or from the existing kernels using the following instructions:
:doc:`guide-conda`.

.. image:: images/kernels.png
   :width: 300
   :align: center

The pre-installed kernels are listed below. The versions of the packages
in these kernels will be occasionally upgraded.

1. Python3 kernel (default)
----------------------------

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
----------------------------

This kernel features the latest version of `Coffea <https://coffeateam.github.io/coffea/>`_ package,
which is regularly updated. In contrtast, the Coffea version in the default
environment is fixed to ``0.7.21``.

.. note::

   If you want to use the ``coffea_latest`` environment but missing some packages,
   please :doc:`contact Purdue AF admins <doc-support>` and we will install them
   for you.

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
-----------------------

This kernel provides an interactive interface to the ROOT command line,
allowing to execute ROOT macros and produce plots inside Jupyter notebooks.

.. seealso::

   :doc:`ROOT C++ notebook demo <demos/root-cpp>`