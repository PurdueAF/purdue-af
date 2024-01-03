How to scale up using Dask Gateway
===================================

.. warning::
    Dask Gateway creates schedulers and workers at the Purdue Hammer cluster via SLURM.
    
    Therefore, all analysis code that uses Dask Gateway must be stored in a storage volume accessible from both Hammer
    cluster, and from Purdue Analysis Facility.
    At the moment, there is only one such volume - **Purdue Depot storage**.
    
    Depot is only accessbile for users with a
    Purdue account, therefore **CERN and FNAL users cannot use Dask Gateway at the moment**.

* Default conda environments ``python3`` and ``python3-ml`` have all necessary software installed.
  If you want to use Dask Gateway in your own environment, make sure that it contains ``dask-gateway``,
  ``ipykernel`` and ``ipywidgets`` packages.
* For more information, refer to `Dask Gateway documentation <https://gateway.dask.org/>`_.


How to use Dask Gateway to create SLURM clusters
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

1. **Initialize ``gateway`` object.**
   It will be used to interact with your Dask clusters.

.. code-block:: python
   
   from dask_gateway import Gateway
   gateway = Gateway()

2. **Configure cluster.**
   There are two ways to configure a Dask cluster, choose what works better in your case:

   1. Using ``options`` object via interactive Jupyter widget
   2. Using keyword arguments (will override ``options``)

.. code-block:: python

   #Changes to parameters in the widget are automatically applied to the "options" object.
   options = gateway.cluster_options()
   options

3. **Create a new cluster.**
   If Slurm job doesn't get scheduled within `cluster_start_timout`, the cluster creation will fail. You can try to increase timeout or use a different queue.

.. code-block:: python
   
   # 1. using "options" object
   cluster = gateway.new_cluster(options)

.. code-block:: python

   # 2. using keywords (will override values set in "options")
   cluster = gateway.new_cluster(
       options, # not required
       conda_env = "/depot/cms/kernels/python3-ml",
       queue = "cms",
       worker_cores = 1,
       worker_memory = 4,
       env = {"KEY1": "VALUE1", "KEY2": "VALUE2"},
       cluster_start_timeout = 60,
   )

Clusters can be scaled either via Jupyter widget or via ``cluster.scale()`` and ``cluster.adapt()`` commands.

**Connect to an existing cluster:**

.. code-block:: python

   # List available clusters
   clusters = gateway.list_clusters()
   print(clusters)
   # Connect to an existing cluster by name
   cluster_name = "d63af7e662e84ac7b89ecb38b524221b"   # paste cluster name here
   cluster = gateway.connect(cluster_name)

4. **Connect a client to a cluster.**

.. code-block:: python
   
   client = cluster.get_client()

.. code-block:: python

   # Or connect to a specific cluster by name:
   cluster_name = "d63af7e662e84ac7b89ecb38b524221b" # paste cluster name here
   client = gateway.connect(cluster_name).get_client()

5. **Shut down cluster.**

.. code-block:: python

   cluster.shutdown()


.. code-block:: python

   # Or shut down a specific cluster by name:
   cluster_name = "d63af7e662e84ac7b89ecb38b524221b" # paste cluster name here
   client = gateway.connect(cluster_name).shutdown()

**Shut down all clusters:**

.. code-block:: python

   for cluster_info in gateway.list_clusters():
       gateway.connect(cluster_info.name).shutdown()