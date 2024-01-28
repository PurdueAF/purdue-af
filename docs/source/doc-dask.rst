Scaling out with Dask
==========================

If your analysis code is written in Python, it is likely that it can be accelerated
using `Dask <https://docs.dask.org/en/stable/>`_ library. Dask includes multiple submodules
with different use cases; here we will focus only on ``dask.distributed`` (or simply ``distributed``)
submodule.

The ``distributed`` package is installed in the default ``Python3`` kernel (Conda environment ``/depot/cms/kernels/python3```).
To use it in your own private kernel: ``conda install distributed``.

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

* ``Client()`` - Dask client connected to a cluster (scheduler). See options below.
* ``func()`` - function to be parallelized.
* ``args`` - list of arguments for which the function will be executed.
* ``futures`` - metadata associated with tasks submited to the Dask clusters via ``client.map()`` command.
* ``results`` - actual results returned once all tasks have been completed


Dask Clusters and Clients
---------------------------

1. Local cluster
^^^^^^^^^^^^^^^^^^^^^^^^^^

Local cluster can be used to parallelize the analysis code over the local CPU cores.
In most cases, the best scaling is achieved when the number of Dask workers
doesn't exceed the number of cores selected at session creation.

.. code-block:: python

   from distributed import LocalCluster, Client
   cluster = LocalCluster()
   cluster.scale(4) # create 4 local workers
   client = Client(cluster)

2. Dask Gateway cluster
^^^^^^^^^^^^^^^^^^^^^^^^^^

Dask Gateway provides a way to scale out to multiple compute nodes, using SLURM 
batch scheduler in the backend.

.. warning::

   Dask Gateway will submit SLURM jobs to the Purdue Hammer cluster.

   Therefore, all analysis code that uses Dask Gateway must be located in
   a storage volume accessible from both the Purdue Analysis Facility and 
   the Hammer cluster.
   
   At the moment, there is only one such volume - Purdue Depot storage.
   Purdue Depot is only accessbile for users with a Purdue account,
   therefore CERN and FNAL users cannot use Dask Gateway at the moment.

Example notebook: :doc:`demos/gateway-cluster`

a. Connecting to a Dask Gateway cluster manually

.. code-block:: python

   from dask_gateway import Gateway
   gateway = Gateway()
   cluster = gateway.new_cluster(...)
   client = cluster.get_client()

b. Connecting to a Dask Gateway cluster automatically

.. code-block:: python

   from dask_gateway import Gateway
   gateway = Gateway()
   clusters = gateway.list_clusters()

   # for example, select the first of existing clusters
   cluster = gateway.connect(clusters[0].name)
   client = cluster.get_client()

