Scaling out with Dask
#######################

If your analysis code is written in Python, it is likely that it can be accelerated
using `Dask <https://docs.dask.org/en/stable/>`_ library. Dask includes multiple submodules
with different use cases; here we will focus only on ``dask.distributed`` (or simply ``distributed``)
submodule.

.. note::

   The ``distributed`` package is already installed in the default ``Python3`` kernel
   (corresponding to Conda environment ``/depot/cms/kernels/python3```).

   To use ``distributed`` in your own private kernel: ``conda install distributed``.

Parallelization example
========================

Below is a simple example of parallelizing execution of a function using Dask.

.. code-block:: python

   from distributed import Client
   client = Client(...)

   def func(x):
      return x*x
   
   args = [1, 2, 3, 4, 5]
   futures = client.map(func, args)
   results = client.gather(futures)

   print(results)
   # [1, 4, 9, 16, 25]

In the code above:

* ``client`` - Dask client connected to a cluster (scheduler). See options below.
* ``func()`` - function to be parallelized.
* ``args`` - list of arguments for which the function will be executed.
* ``futures`` - metadata associated with tasks submited to the Dask clusters via ``client.map()`` command.
* ``results`` - actual results returned once all tasks have been completed

.. tip::

   Before enabling parallelization via Dask client, make sure that your code
   works by running it on a small set of arguments sequentially:
   
   .. code-block:: python

      results = []
      for arg in args:
         results.append(func(arg))

Dask Clusters and Clients
===========================

1. Local cluster
-------------------

Local cluster can be used to parallelize the analysis code over the local CPU cores.
The number of workers that you can create is limited by the amount of resources
selected during session creation (**up to 64 cores** and **up to 128 GB RAM**).

.. admonition:: LocalCluster setup
   :class: toggle

   .. code-block:: python

      from distributed import LocalCluster, Client
      cluster = LocalCluster()
      cluster.scale(4) # create 4 local workers
      client = Client(cluster)

2. Dask Gateway cluster
------------------------

Dask Gateway provides a way to scale out to multiple compute nodes,
using either SLURM batch scheduler or Kubernetes in the backend. With Dask Gateway, you
should be able to quickly scale **up to 100 cores** and **400 GB RAM** or more,
depending on availability of resources.

.. note::

   Default Python3 kernel / conda environment has all necessary software installed.
   If you want to use Dask Gateway in your own custom environment, make sure
   that it contains ``dask-gateway``, ``ipykernel`` and ``ipywidgets`` packages.

Please refer to the following page for detailed documentation about
Dask Gateway at Purdue Analysis Facility: :doc:`guide-dask-gateway`.