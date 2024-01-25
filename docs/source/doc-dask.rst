Scaling out with Dask
==========================

If your analysis code is written in Python, it is likely that it can be accelerated
using `Dask <https://docs.dask.org/en/stable/>`_ library. Dask includes multiple submodules
with different use cases; here we will focus only on `dask.distributed` (or simply `distributed`)
submodule.

Below is a simple example of parallelizing execution of a function using Dask.

.. code-block:: python

   from dask.distributed import Client
   # Create a Dask client - this can be done in multiple ways (see below)
   client = Client(...)

   # This is the function that we want to parallelize
   def func(x):
      return x*x
   
   # These are arguments over which the function needs to be executed
   args = [1, 2, 3, 4, 5]

   # `map()` creates execution tasks that will run in parallel for multiple arguments at the same time.
   # `futures` returns not the actual results, but simply metadata associated with the submitted tasks.
   futures = client.map(func, args)

   # To retreive results once they are computed:
   results = client.gather(futures)

   print(results)
   # [1, 4, 9, 16, 25]


Dask Clusters and Clients
---------------------------

1. Local cluster

2. Dask Gateway cluster

   a. Connecting to a Dask Gateway cluster manually
   b. Connecting to a Dask Gateway cluster automatically

:doc:`demos/gateway-cluster`
