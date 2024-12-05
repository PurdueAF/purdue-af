.. _doc-storage:

Storage and file sharing
==================================

The Purdue Analysis Facility provides access to multiple storage options.

.. raw:: html

   <div class="wy-table-responsive">

.. list-table:: Storage Options
   :header-rows: 1
   :widths: 1 2 1 4 1 1 1

   * - Storage volume
     - Path
     - Size
     - Use cases
     - Access mode
     - Mounted in Slurm jobs
     - Mounted in k8s Dask workers
     - Available for users without Purdue account
   * - AF home storage
     - ``/home/<username>/``
     - 25 GB
     - This is JupyterLab's default directory, suitable for storing small files. Deleted after 6 months of inactivity unless requested otherwise.
     - Read/write
     - ❌
     - ❌
     - ✅
   * - AF work storage
     - ``/work/users/<username>/``
     - 100 GB
     - Collaborative work; directory is readable by all users by default (permissions can be adjusted); deleted after 6 months of inactivity unless requested otherwise.
     - Read/write
     - ❌
     - ✅
     - ✅
   * - AF shared project storage
     - ``/work/projects/``
     - up to 1 TB
     - Shared directories for collaborative projects; created upon request.
     - Read/write
     - ❌
     - ✅
     - ✅
   * - Purdue Depot storage
     - ``/depot/cms/`` and ``/home/<username>/depot/``
     - up to 1 TB
     - Storing job outputs and small datasets; read/write access for Purdue users; read-only for others.
     - Read/write for Purdue users, read-only for others
     - ✅
     - ✅
     - ❌
   * - Purdue EOS
     - ``/eos/purdue/`` and ``/home/<username>/eos-purdue/``
     - up to 100 TB
     - Storage for large datasets; read-only access; includes CMS datasets and user Grid directories.
     - Read/write for Purdue users, read-only for others
     - ✅
     - ✅
     - ❌
   * - CVMFS
     - ``/cvmfs/``
     - N/A
     - Installation of CMSSW releases; read-only access.
     - Read-only
     - ✅
     - ✅
     - ✅
   * - CERNBox (CERN EOS)
     - ``/eos/cern/`` and ``/home/<username>/eos-cern/``
     - 
     - Connect to private CERNBox directory; read/write access; enable by running ``eos-connect`` command.
     - Read/write
     - ❌
     - ❌
     - ✅

.. raw:: html

   </div>

.. warning::
   
    Access to CERNBox is not enabled by default. To set it up, please follow these instructions:
    :doc:`guide-cern-eos`

* **Other options:**

  * **Git** functionality is enabled, users can use GitHub or GitLab to store and share their work.
    The Git extension located in the left sidebar allows to work with repositories interactively  (commit, push, pull, etc.).
  * **XRootD client** is installed and can be used to access data stored at other CERN sites.

* **Subscribing datasets with Rucio**
    :doc:`guide-rucio`
