.. _doc-storage:

.. attention::

    **Updated: January 14, 2025**

    The Purdue EOS storage servers are down since December 23, 2024.

    All EOS directories are affected, including ``/store/user``, ``/store/data``, and ``/store/mc``.
    
    CMS datasets can still be accessed from other CMS sites via XRootD or XCache.
    Datasets with the only copy at T2_Purdue, as well as any private data at EOS
    are not accessible at the moment; we hope to recover all data by January 17th.

    Outputs of CRAB jobs executed since December 23rd are stored on fallback servers,
    please contact the site support if you need to access them.

    All other storage (``/home/``, ``/work/``, Depot) remains fully operational.

    We will keep you updated on the progress of the recovery.

Storage volumes
==================================

.. raw:: html

   <div class="wy-table-responsive">

.. list-table:: 
   :header-rows: 1
   :widths: 1 2 1 3 2 1 1 1

   * - Storage volume
     - Path
     - Size
     - Use cases
     - Access mode
     - Mounted in Slurm jobs
     - Mounted in k8s Dask workers
     - Writable by users w/o Purdue account
   * - AF home storage
     - ``/home/<username>/``
     - 25 GB
     - JupyterLab's default directory, suitable for storing small files.
     - Read/write
     - ❌
     - ❌
     - ✅
   * - Purdue Depot storage
     - ``/depot/cms/``
     - up to 1 TB
     - Best as a working directory for Purdue users, because Depot is mounted
       to all clusters and Dask workers.
     - Read/write for Purdue users, read-only for others
     - ✅
     - ✅
     - ❌
   * - AF work storage
     - ``/work/users/<username>/``
     - 100 GB
     - Best for collaboration with non-Purdue users and writing outputs
       from Dask Gateway workers with Kubernetes backend.
       Readable by all users by default (permissions can be adjusted).
     - Read/write
     - ❌
     - ✅
     - ✅
   * - AF shared project storage
     - ``/work/projects/``
     - up to 1 TB
     - Project directories for collaborative work created upon request.
     - Read/write
     - ❌
     - ✅
     - ✅
   * - Purdue EOS
     - ``/eos/purdue/``
     - up to 100 TB
     - Storage for large job outputs and CMS datasets. Users can request
       creation of personal Grid sub-directories tied to CERN account.
     - Read-only
     - ✅
     - ✅
     - ❌
   * - CVMFS
     - ``/cvmfs/``
     - N/A
     - Distributed CernVM file system, primarily used to install CMSSW releases.
       Can be used to load Apptainer images or LCG releases.
     - Read-only
     - ✅
     - ✅
     - ❌
   * - CERNBox (CERN EOS)
     - ``/eos/cern/``
     - 
     - Private CERNBox directory, useful for collaboration outside of Purdue AF.
       To enable access, follow these instructions: :doc:`guide-cern-eos`.
     - Read/write
     - ❌
     - ❌
     - ✅

.. raw:: html

   </div>

.. warning::
   
   Avoid writing many files to Depot at the same time, as it may slow
   Depot down for everyone. If your jobs produce large outputs,
   it is recommended to first save them into ``/tmp/<username>`` at
   individual Slurm jobs / Dask workers, and then copy over to EOS
   using ``gfal`` or ``xrdcp`` commands: :doc:`doc-data-access`.

**Other options:**

* **Git** functionality is enabled, users can use GitHub or GitLab to store and share their work.
  The Git extension located in the left sidebar allows to work with repositories interactively  (commit, push, pull, etc.).
* **XRootD client** is installed and can be used to access data stored at other CERN sites.
