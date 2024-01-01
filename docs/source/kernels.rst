Software (Jupyter kernels)
==========================

The Purdue Analysis Facility provides several Jupyter kernels with pre-installed analysis software.
Users can also create their own kernels from scratch or from the existing kernels using the following instructions:
`How to create and share Conda environments and Jupyter kernels <fixme-link>`_ 

The pre-installed kernels are listed below. The versions of the packages in these kernels are not fixed,
and will be occasionally upgraded.

.. ![Untitled](https://s3-us-west-2.amazonaws.com/secure.notion-static.com/ec8b6eb2-fbe4-4958-b1a9-19f773c00680/Untitled.png)
.. TODO: add screenshot of kernels

Python3 kernel (default)
~~~~~~~~~~~~~~~~~~~~~~~~~

This kernel is designed for typical pythonic analysis workflows which do not include machine learning.
The kernel is based on Python 3.10. The following packages are installed:

* Scientific computing and data analysis: ``numpy``, ``scipy``, ``pandas``, ``awkward``, ``numba``,
``scikit-learn``, ``uncertainties``, ``lmfit``
* High energy physics tools: ``ROOT``, ``uproot``, ``coffea``, ``vector``, ``hist``, ``pyhf``
* Plotting: ``matplotlib``, ``mplhep``, ``plotly``
* Distributed computing: ``dask``, ``dask[distributed]``, ``dask[dataframe]``, ``dask-jobqueue``
* Other tools: ``xrootd``, ``pytest``, ``yaml``, ``tqdm``

Python3 kernel [ML]
~~~~~~~~~~~~~~~~~~~~~~~~~

This kernel includes all of the packages included into the default kernel,
and adds the most popular machine learning packages:

* ``tensorflow``
* ``pytorch`` and ``pytorch-geometric``
* ``keras``
* ``xgboost``

ROOT C++ kernel
~~~~~~~~~~~~~~~~~~~~~~~~~

This kernel provides an interactive interface to the ROOT command line,
allowing to execute ROOT macros and produce plots inside Jupyter notebooks.
  `ROOT C++ notebook demo <link>`_