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
should be able to quickly scale **up to 100-200 cores** and **400-800 GB RAM** or more,
depending on availability of resources.

.. note::

   Default Python3 kernel / conda environment has all necessary software installed.
   If you want to use Dask Gateway in your own custom environment, make sure
   that it contains ``dask-gateway``, ``ipykernel`` and ``ipywidgets`` packages.

.. .. warning::

..    Dask Gateway will submit SLURM jobs to the Purdue Hammer cluster.
..    Therefore, **all analysis code that uses Dask Gateway must be located
..    in Purdue Depot storage**, in order to be accessible by Dask workers.
   
..    Currenlty, Depot is only writeable by Purdue users, but not by CERN or FNAL users.

2.1 Gateway Cluster creation
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Purdue Analysis Facility provides two ways to create Dask Gateway clusters:

* From an interactive JupyterLab extension
* Manually from a Jupyter Notebook or Python script

The instructions and caveats for these methods are described below.

.. tabs::

   .. group-tab:: Interactive JupyterLab extension

      1. Click on the Dask logo in the left sidebar of JupyterLab interface.
      2. Click on ``[+ NEW]`` button to open the dialog window with cluster settings.
      3. In the dialog window, select cluster type, kernel, and desired worker resources.
      4. Click the ``[Apply]`` button and wait for ~1 min, the cluster info will appear in the interface.
      5. The sidebar should automatically connect to Dask dashboards;
         you can open different dashboards by clicking on yellow buttons in the sidebar,
         and rearrange the tabs as desired.

   .. group-tab:: Jupyter Notebook or Python script

      .. code-block:: python

         import os
         import dask_gateway
         from dask_gateway import Gateway

         # To submit jobs via SLURM (Purdue users only!)
         gateway = Gateway()

         # To submit jobs via Kubernetes (all users)
         # gateway = Gateway(
         #     "http://dask-gateway-k8s.geddes.rcac.purdue.edu/",
         #     proxy_address="traefik-dask-gateway-k8s.cms.geddes.rcac.purdue.edu:8786",
         # )

         # You may need to update some environment variables before creating a cluster.
         # For example:
         os.environ["X509_USER_PROXY"] = "/path-to-voms-proxy/"

         # Create the cluster
         cluster = gateway.new_cluster(
            conda_env = "/depot/cms/kernels/python3", # path to conda env
            worker_cores = 1,    # cores per worker
            worker_memory = 4,   # memory per worker in GB
            env = dict(os.environ), # pass environment as a dictionary
         )

         # If working in Jupyter Notebook, the following will create a widget
         # which can be used to scale the cluster interactively:
         cluster
     

.. .. admonition:: Dask Gateway cluster setup (example notebook)
..    :class: toggle

..    :doc:`demos/gateway-cluster`

..    You can copy this notebook from ``/depot/cms/purdue-af/purdue-af-demos/gateway-cluster.ipynb``
..    and customize it for your purposes.

2.2 Shared environments and storage volumes in Dask Gateway
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

* **Conda environments / Jupyter kernels**
  
   .. tabs::

      .. group-tab:: Interactive JupyterLab extension

         The Conda environment / Jupyter kernel can be selected from a drop-down list
         in the dialog window that appears when you click on ``[+NEW]`` button.
  
         To make your Conda environment appear as a kernel,
         it must have the ``ipykernel`` package installed.

      .. group-tab:: Jupyter Notebook or Python script
         
         The path to conda environment is specified in the ``conda_env``
         argument of ``new_cluster()``:

         .. code-block:: python

            cluster = new_cluster(
               conda_env = "/depot/cms/kernels/python3",
               # ...
            )

*  **Shared storage**

   Dask workers have the same permissions as the user that creates them.
   You can use this to your advantage if your workers read/write data to/from storage locations. 

   Refer to the following table to decide which Dask Gateway setup works best in your case:

   +----------+----------------------+---------------------------+------------------------------+
   |          | SLURM (Purdue users) | Kubernetes (Purdue users) | Kubernetes (CERN/FNAL users) |
   +==========+======================+===========================+==============================+
   | *Depot*  | read / write         | read / write              | read-only                    |
   +----------+----------------------+---------------------------+------------------------------+
   | */work/* | no access            | read / write              | read / write                 |
   +----------+----------------------+---------------------------+------------------------------+
   | *EOS*    | read-only            | read-only                 | read-only                    |
   +----------+----------------------+---------------------------+------------------------------+

*  **Environment variables**

   Passing environment variables to workers can be beneficial in various ways, for example:

   * Enable imports from local Python (sub)modules by amending the ``PYTHONPATH`` variable.
   * Enable imports from C++ libraries by amending the ``LD_LIBRARY_PATH`` variable.
   * Allow workers to read data via XRootD by specifying path to VOMS proxy via ``X509_USER_PROXY`` variable.

   These and other environment variables can be passed to Dask workers as follows:

   .. tabs::

      .. group-tab:: Interactive JupyterLab extension

         When a Dask Gateway cluster is created via the JupyterLab extension,
         there is no direct interface to pass environment to workers.

         Instead, we use the following workaround to override the
         worker environment:

         1. Create a file ``~/.config/dask/labextension.yaml``
         2. Add any environment variables in the following way:

            .. code-block:: yaml

               # contents of labextension.yaml
               labextension:
                  env_override:
                     KEY1: VALUE1
                     X509_USER_PROXY: "/path-to-proxy/"
                     # any other variables..
         
         3. **Shut down and restart the Analysis Facility session**
         4. Create a new cluster by clicking the ``[+NEW]`` button in the left sidebar.

      .. group-tab:: Jupyter Notebook or Python script

         The ``gateway.new_cluster()`` command takes ``env`` argument which can be used
         to pass any set of environment variables to workers. The most straightforward
         way to use this is to pass the entire local environment as follows:

         .. code-block:: python

            cluster = gateway.new_cluster(
               #...
               env = dict(os.environ)
            )

         .. important::

            For CERN and FNAL users, the dictionary passed to ``env`` argument must
            contain elements ``"NB_UID"`` and ``"NB_GID"``. **This is already satisfied when
            you pass** ``env = dict(os.environ)``, **so no further action is needed.**
            
            However, if you want to pass a custom environment
            to workers, you can add the required elements as follows:

            .. code-block:: python

               env = {
                  "NB_UID": os.environ["NB_UID"],
                  "NB_GID": os.environ["NB_GID"],
                  # other environment variables...
               }  

2.3 Cluster lifetime and timeouts
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

* Cluster creation will fail if the scheduler doesn't start in **2 minutes**.
  If this happens, try to resubmit the cluster.
* Once created, Dask scheduler and workers will persist for **1 day**.
* If the notebook from which the Dask Gateway cluster was created is
  terminated, the cluster and all its workers will be killed after **5 minutes**.


2.4 Connecting a Client to a Dask Gateway cluster
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

In the main analysis code, you can connect to the Gateway cluster either
by manually pasting the cluster name, or by selecting an existing cluster
automatically.

.. tabs::

   .. tab:: **Connecting manually**

      .. note::

         If you created the cluster via the interactive extension, you can obtain
         the client code simply by clicking on the ``<>`` symbol in the cluster widget.
         This action will paste the client code into a new cell in the most
         recently used Jupyter notebook.

      .. code-block:: python

         from dask_gateway import Gateway

         # If submitting workers as SLURM jobs (Purdue users only):
         gateway = Gateway()

         # If submitting workers as Kubernetes pods (all users):
         # gateway = Gateway(
         #     "http://dask-gateway-k8s.geddes.rcac.purdue.edu/",
         #     proxy_address="traefik-dask-gateway-k8s.cms.geddes.rcac.purdue.edu:8786",
         # )

         # To find the cluster name:
         print(gateway.list_clusters())

         # replace with actual cluster name:
         cluster_name = "17dfaa3c10dc48719f5dd8371893f3e5"
         client = gateway.connect(cluster_name).get_client()

   .. tab:: **Connecting automatically**

      .. code-block:: python

         from dask_gateway import Gateway

         # If submitting workers as SLURM jobs (Purdue users only):
         gateway = Gateway()

         # If submitting workers as Kubernetes pods (all users):
         # gateway = Gateway(
         #     "http://dask-gateway-k8s.geddes.rcac.purdue.edu/",
         #     proxy_address="traefik-dask-gateway-k8s.cms.geddes.rcac.purdue.edu:8786",
         # )

         clusters = gateway.list_clusters()
         # for example, select the first of existing clusters
         cluster_name = clusters[0].name
         cluster = gateway.connect(cluster_name).get_client()

      .. caution::

         If you have more than one Dask Gateway cluster running, automatic detection
         may be ambiguous.

