.. _doc-storage:

Storage volumes
==================================

Overview
--------

The following table summarizes size, access modes, and accessibility of each storage volume (scroll sideways for details).

.. raw:: html

   <div class="wy-table-responsive">

.. list-table:: 
   :header-rows: 1
   :widths: 1 2 1 2 1 1 1

   * - Storage volume
     - Path
     - Size
     - Access mode
     - Mounted in Slurm jobs and Dask/Slurm workers
     - Mounted in Dask/k8s workers
     - Writable by users w/o Purdue account
   * - AF home storage
     - ``/home/<username>/``
     - 25 GB
     - Read/write
     - ❌
     - ❌
     - ✅
   * - Purdue Depot storage
     - ``/depot/cms/``
     - up to 1 TB
     - Read/write for Purdue users, read-only for others
     - ✅
     - ✅
     - ❌
   * - AF work storage
     - ``/work/users/<username>/``
     - 100 GB
     - Read/write
     - ❌
     - ✅
     - ✅
   * - AF shared project storage
     - ``/work/projects/``
     - up to 1 TB
     - Read/write
     - ❌
     - ✅
     - ✅
   * - Purdue EOS
     - ``/eos/purdue/``
     - up to 100 TB
     - Read-only
     - ✅
     - ✅
     - ❌
   * - CVMFS
     - ``/cvmfs/``
     - N/A
     - Read-only
     - ✅
     - ✅
     - ❌
   * - CERNBox (CERN EOS)
     - ``/eos/cern/``
     - N/A
     - Read/write
     - ❌
     - ❌
     - ✅

.. raw:: html

   </div>


Which storage volume should I use?
-----------------------------------

.. warning::

   Your ``/home/<username>/`` directory (root directory of JupyterLab file browser) has a strict quota of 25 GB.
   If you go over this limit, you will not be able to start a session on Purdue AF.
   Rather than storing your data, Pixi or Conda environments, etc. in your home directory, consider using other storage volumes listed below.

   You can check your current ``/home/`` directory usage with the following command: ``bash du -sh $HOME``.


Below are common storage use cases with recommendations on which storage volume to use.

- **Transferring official CMS datasets to Purdue:**

  - Locate the dataset using `DAS (CMS Data Aggregation System) <https://cmsweb.cern.ch/das/>`_
  - Use :doc:`Rucio <guide-rucio>` to 'subscribe' dataset to Purdue for a *limited* amount of time.
  - The dataset will be copied to the **Purdue EOS** storage and appear under ``/eos/purdue/store/mc/`` or ``/eos/purdue/store/data/``.

- **Saving outputs of CRAB jobs** (for example for :doc:`guide-mc-gen`)

  - The outputs of CRAB jobs will be written to your Grid directory, which is ``/eos/purdue/store/user/<cern-username>``.
    *Note: CERN username is different from Purdue username!*
  - The Grid directory at Purdue EOS is created only for Purdue-affiliated users. This must be indicated when creating Purdue Tier-2 account.
  - If you can't see your Grid directory under ``/eos/purdue/store/user/``, please :doc:`contact support <doc-support>`.

- **Processing ("skimming") CMS datasets:**

  - The best storage volume to use will depend on the size of the output.
  - For large outputs (over 100 GB), it is recommended to save outputs to **Purdue EOS**.
    Since Purdue EOS is not directly writeable, this can be achieved by saving outputs into ``/tmp/<username>/`` and then copying over to Purdue EOS using ``gfal`` or ``xrdcp`` commands.
  - For small outputs (under 100 GB):

    - Purdue users should use **Depot** (``/depot/cms``). If the outputs need to be accessible by other users, use a group directory (e.g. ``/depot/cms/top/``).
    - Non-Purdue users should use **/work/ storage**: ``/work/users/<username>/`` or ``/work/projects/<project-name>``.

- **Storing custom Pixi or Conda environments:**

  - In order for Pixi or Conda environments to appear as JupyterLab kernels, they must be stored in publicly readable directories, so ``/depot/cms/user/`` will NOT work.
  - Possible locations for your Pixi or Conda environments are:

    - group directories at Depot (for example, ``/depot/cms/top/``)
    - personal directories at work storage: ``/work/users/<username>/``
    - shared project directories at work storage: ``/work/projects/<project-name>/``

  - If using Slurm jobs or Dask Gateway workers, make sure that the directory where Pixi or Conda environments are stored is visible from them (see table above).


.. warning::
   
   Avoid writing many files to Depot at the same time, as it may slow
   Depot down for everyone. If your jobs produce large outputs,
   it is recommended to first save them into ``/tmp/<username>`` at
   individual Slurm jobs / Dask workers, and then copy over to EOS
   using ``gfal`` or ``xrdcp`` commands: :doc:`doc-data-access`.

Other options
--------------

* **Git** functionality is enabled, users can use GitHub or GitLab to store and share their work.
  The Git extension located in the left sidebar allows to work with repositories interactively  (commit, push, pull, etc.).
* **XRootD client** is installed and can be used to access data stored at other CERN sites.
