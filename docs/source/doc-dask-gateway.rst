Dask Gateway at Purdue AF
#########################################

Dask Gateway is a service that allows to manage Dask clusters in a milti-tenant environment
such as the Purdue Analysis Facility.

To make Dask Gateway useful in a variety of analysis workflows, we provide four ways to work with it:

* The Dask Gateway cluster creation can be done in two ways:

  * **Interactively** from the Dask Labextension interface
  * **Manually** in a Jupyter Notebook or in a Python script

* For each of these methods, we allow to create two types of clusters:

  * **Dask Gateway cluster with SLURM backend**: workers are submitted to Purdue Hammer cluster.
    This is available to **Purdue users only** due to Purdue data access policies.

    With this method, users can potentially create **hundreds of workers**, but in practice
    requesting more than 100 workers is usually associated with some wait time due to competiton with
    CMS production jobs and other users.

  * **Dask Gateway cluster with Kubernetes backend**: workers are submitted to Purdue Geddes cluster.
    This is available to **all users**.

    With this method, the workers are scheduled almost instantly, but for now we restrict
    the total per-user resource usage to **100 cores, 400 GB RAM** due to limited resources
    in the Analysis Facility.

  The pros and cons of the Dask Gateway backends are summarized in the following table:

  +----------+-----------------------------+---------------------------------+
  |          | Dask Gateway + SLURM        | Dask Gateway + Kubernetes       |
  +==========+=============================+=================================+
  | **Pros** | * SLURM is familiar to      | * Fast scheduling of resources  |
  |          |   current users             |                                 |
  |          |                             |                                 |
  |          | * Easy to access logs and   | * Detailed monitoring           |
  |          |   worker info via ``squeue``|                                 |
  |          |                             | * Available to CERN/FNAL users  |
  |          |                             |                                 |
  +----------+-----------------------------+---------------------------------+
  | **Cons** | * Unavailable to CERN/FNAL  | * Limited total amount of       |
  |          |   users                     |   resources                     |
  |          |                             |                                 |
  |          | * Scheduling workers can be | * Retreiving detailed worker    |
  |          |   slow due to competition   |   info can be non-trivial for   |
  |          |   with CMS production jobs  |   users (but easy for admins)   |
  +----------+-----------------------------+---------------------------------+


1. Creating Dask Gateway clusters 
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

This section contains the instructions for creating Dask Gateway clusters using the methods described above.

.. tabs::

   .. group-tab:: Interactive JupyterLab extension

      1. Click on the Dask logo in the left sidebar of JupyterLab interface.
      2. Click on ``[+ NEW]`` button to open the dialog window with cluster settings.
      3. In the dialog window, select cluster type, kernel, and desired worker resources.
      4. Click the ``[Apply]`` button and wait for ~1 min, the cluster info will appear in the sidebar.
      5. The sidebar should automatically connect to Dask dashboards;
         you can open different dashboards by clicking on yellow buttons in the sidebar,
         and rearrange the tabs as desired.
      
      .. image:: images/dask-gateway.png
         :width: 700
         :align: center

   .. group-tab:: Jupyter Notebook or Python script

      To create a Dask Gateway cluster manually, you need to connect to the Gateway server
      via a ``Gateway`` object, and then use ``Gateway.new_cluster()`` method.

      Calling ``Gateway()`` without arguments will connect you to the server with **SLURM backend**.
      In order to use the **Kubernetes** backend, you need to specify the server URL explicitly
      (see code below).

      While it is possible to create a cluster in a Python script, we recommend that you instead
      do it from a separate Jupyter Notebook - that way the same cluster can be reused multiple
      times without restarting.

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

2. Shared environments and storage volumes 
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

There are multiple ways to ensure that the workers have access to specific storage volumes,
Conda environments, Python packages, C++ libraries, etc.

*  **Shared storage**

   Dask workers have the same permissions as the user that creates them.
   You can use this to your advantage if your workers read/write data to/from
   storage locations.

   Refer to the following table to decide which Dask Gateway setup works best in your case:

   +------------+---------------+--------------------+--------------------+
   |            | SLURM workers | Kubernetes workers | Kubernetes workers |
   |            |               |                    |                    |
   |            | (Purdue users)| (Purdue users)     | (CERN/FNAL users)  |
   +============+===============+====================+====================+
   | **/home/** | no access     | no access          | no access          |
   +------------+---------------+--------------------+--------------------+
   | **/work/** | no access     | read / write       | read / write       |
   +------------+---------------+--------------------+--------------------+
   | **Depot**  | read / write  | read / write       | read-only          |
   +------------+---------------+--------------------+--------------------+
   | **CVMFS**  | read-only     | read-only          | read-only          |
   +------------+---------------+--------------------+--------------------+
   | **EOS**    | read-only     | read-only          | read-only          |
   +------------+---------------+--------------------+--------------------+


* **Conda environments / Jupyter kernels**

  Any Conda environment that is used in your analysis can be propagated to Dask workers.
  The only caveat is that the workers must have read access to the storage volume where the
  environment is stored (see table above). For example, SLURM workers will not be able to see
  Conda environments located in ``/work/`` storage.

  .. tabs::

     .. group-tab:: Interactive JupyterLab extension

        The Conda environment / Jupyter kernel can be selected from a drop-down list
        in the dialog window that appears when you click on ``[+NEW]`` button.

        To make your Conda environment appear as a kernel,
        it must have the ``ipykernel`` package installed.

        .. image:: images/dask-gateway-dialog.png
           :width: 400
           :align: center

     .. group-tab:: Jupyter Notebook or Python script
         
        The path to conda environment is specified in the ``conda_env``
        argument of ``new_cluster()``:

        .. code-block:: python

           cluster = gateway.new_cluster(
              conda_env = "/depot/cms/kernels/python3",
              # ...
           )

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

            os.environ["X509_USER_PROXY"] = "/path-to-proxy"

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

3. Monitoring 
^^^^^^^^^^^^^^^

Monitoring your Dask jobs is possible in two ways:

1. Via Dask dashboard which is created for each cluster (see instructions below).
2. Via the general Purdue AF monitoring page, in the "Slurm metrics" and "Dask metrics" sections
   of the |open_dashboard|.

.. |open_dashboard| raw:: html

   <a href="https://cms.geddes.rcac.purdue.edu/grafana/d/purdue-af-dashboard/purdue-analysis-facility-dashboard" target="_blank">
      monitoring dashboard
   </a>

Instructions to open Dask cluster dashboards for different Gateway setups:

.. tabs::

  .. group-tab:: Interactive JupyterLab extension

     When a cluster is created via the Dask Labextension interface,
     the extension should connect to monitoring dashboards automatically;
     you can open various dashboards by clicking on the yellow buttons in the sidebar.

     Alternatively, you can copy the URL from the window at the top of the Labextension
     sidebar, and open the Dask dashboard in a separate web browser tab.

     .. image:: images/dask-gateway.png
        :width: 700
        :align: center

  .. group-tab:: Jupyter Notebook or Python script
         
     When a cluster is created in a Jupyter Notebook, you can extract the link to the dashboard
     either from a Dask Gateway widget, or from ``cluster.dashboard_link``.

     To create a widget, simply execute a cell containing a reference to the cluster object,
     as shown in the screenshot.

     .. image:: images/dask-gateway-widget.png
        :width: 700
        :align: center


4. Cluster discovery and connecting a client 
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

In general, connecting a client to a Gateway cluster is done as follows:

.. code-block:: python

    client = cluster.get_client()

However, this implies that ``cluster`` refers to an already existing object.
This is true if the cluster was created in the same Notebook / Python script,
but in most cases we recommend that the cluster is kept separate from the clients.

Below are the different ways to connect a client to a cluster created elsewhere:

.. tabs::

   .. tab:: **Automatic cluster discovery**

      This snippet allows to discover the cluster and connect to it automatically,
      as long as the cluster exists.


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

   .. tab:: **Client code injection from extension**

      If you created the cluster via the interactive extension, you can obtain
      the client code simply by clicking on the ``<>`` symbol in the cluster widget.
      This action will paste the client code into a new cell in the most
      recently used Jupyter notebook.

      .. image:: images/dask-gateway-labextension-widget.png
         :width: 300
         :align: center

      .. image:: images/dask-gateway-code-injection.png
         :width: 700
         :align: center

   .. tab:: **Manual connection**

      This is the most straightforward method of connecting to a specific cluster,
      it may be benefitial if you have more than one cluster running and need to ensure
      that you are connecting to a correct one.

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


5. Cluster lifetime and timeouts
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

* Cluster creation will fail if the scheduler doesn't start in **2 minutes**.
  If this happens, try to resubmit the cluster.
* Once created, Dask scheduler and workers will persist for **1 day**.
* If the notebook from which the Dask Gateway cluster was created is
  terminated, the cluster and all its workers will be killed after **1 hour**.
