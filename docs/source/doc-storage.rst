.. _doc-storage:

Storage and file sharing
==================================

The Purdue Analysis Facility provides access to multiple storage options.

.. list-table:: Storage Options
   :header-rows: 1
   :widths: 20 40 20 60

   * - Storage Volume
     - Path
     - Size
     - Use Cases
   * - AF home storage
     - ``/home/<username>/``
     - 25 GB
     - This is JupyterLab's default directory, suitable for storing small files. Deleted after 6 months of inactivity unless requested otherwise.
   * - AF work storage
     - ``/work/users/<username>/``
     - 100 GB
     - Collaborative work; directory is readable by all users by default (permissions can be adjusted); deleted after 6 months of inactivity unless requested otherwise.
   * - AF shared project storage
     - ``/work/projects/``
     - Varies
     - Shared directories for specific projects; created upon request by the project's Principal Investigator.
   * - Purdue Depot storage
     - ``/depot/cms/`` and ``/home/<username>/depot/``
     - Several terabytes
     - Storing job outputs and small datasets; read/write access for Purdue users; read-only for others.
   * - Purdue EOS
     - ``/eos/purdue/`` and ``/home/<username>/eos-purdue/``
     - Large datasets
     - Storage for large datasets; read-only access; includes CMS datasets and user Grid directories.
   * - CVMFS
     - ``/cvmfs/``
     - N/A
     - Installation of CMSSW releases; read-only access.
   * - CERNBox (CERN EOS)
     - ``/eos/cern/`` and ``/home/<username>/eos-cern/``
     - 
     - Connect to private CERNBox directory; read/write access; enable by running ``eos-connect`` command.

.. warning::
   
    Access to CERNBox is not enabled by default. To set it up, please follow these instructions:
    :doc:`guide-cern-eos`

* **Other options:**

  * **Git** functionality is enabled, users can use GitHub or GitLab to store and share their work.
    The Git extension located in the left sidebar allows to work with repositories interactively  (commit, push, pull, etc.).
  * **XRootD client** is installed and can be used to access data stored at other CERN sites.

* **Subscribing datasets with Rucio**
    :doc:`guide-rucio`
