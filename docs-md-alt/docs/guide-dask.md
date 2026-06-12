# Scaling out with Dask

If your analysis code is written in Python, it is likely that it can be accelerated
using the [Dask](https://docs.dask.org/en/stable/) library. Dask includes multiple
submodules with different use cases; here we focus only on the `dask.distributed`
(or simply `distributed`) submodule.

The `distributed` package is already present in the
[global Pixi environment](software.md). You can add it to your own Pixi environment
by running `pixi add distributed` in your Pixi project directory.

## Parallelization example

Below is a simple example of parallelizing the execution of a function using Dask:

```python
from distributed import Client
client = Client(...)

def func(x):
    return x*x

args = [1, 2, 3, 4, 5]
futures = client.map(func, args)
results = client.gather(futures)

print(results)
# [1, 4, 9, 16, 25]
```

In the code above:

* `client` — Dask client connected to a cluster (scheduler). See options below.
* `func()` — function to be parallelized.
* `args` — list of arguments for which the function will be executed.
* `futures` — metadata associated with the tasks submitted to the Dask cluster via
  the `client.map()` command.
* `results` — actual results, returned once all tasks have been completed.

!!! tip

    Before enabling parallelization via the Dask client, make sure that your code
    works by running it on a small set of arguments sequentially:

    ```python
    results = []
    for arg in args:
        results.append(func(arg))
    ```

## Dask clusters and clients

### 1. Local cluster

A local cluster can be used to parallelize the analysis code over local CPU cores.
The number of workers that you can create is limited by the amount of resources
selected during session creation (**up to 128 cores** and **up to 128 GB RAM**).

??? note "LocalCluster setup"

    ```python
    from distributed import LocalCluster, Client
    cluster = LocalCluster()
    cluster.scale(4) # create 4 local workers
    client = Client(cluster)
    ```

### 2. Dask Gateway cluster

Dask Gateway provides a way to scale out to multiple compute nodes, using either
the Slurm batch scheduler or Kubernetes in the backend. With Dask Gateway, you
should be able to quickly scale **up to 200 workers (200 cores, 1.2 TB RAM)** with
the Kubernetes backend, and to hundreds of workers with the Slurm backend,
depending on the availability of resources.

Please refer to the following page for detailed documentation about Dask Gateway
at the Purdue Analysis Facility: [Dask Gateway at Purdue AF](guide-dask-gateway.md).

!!! note "See also"

    * [Dask Gateway cluster setup (demo notebook)](demos/gateway-cluster.md)
    * [Scaling out — overview of all methods](scaling-out.md)
