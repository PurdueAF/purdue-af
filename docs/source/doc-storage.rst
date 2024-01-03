.. _doc-storage:

Storage, data access, file sharing
==================================

The Purdue Analysis Facility provides access to multiple storage options:

* **Private personal storage:** 25GB at ``/home/<username>/``.
  The JupyterLab file browser is based in this directory.
  This directory will be deleted after 6 months of inactivity, unless requested otherwise.
* **Public personal storage:** 100GB at ``/work/users/<username>/``.
  By default, the directory is readable by all Analysis Facility users, and therefore can be used for collaboration
  and sharing your work (permissions can be adjusted).
  This directory will be deleted after 6 months of inactivity, unless requested otherwise.
* **Shared storage:** Shared directories can be created for specific projects in ``/work/projects/`` by
  the Analysis Facility administrators at the request of the project's PI. 
* **External storage:**
  * **Purdue Depot**: shared project space at Purdue, which can be used to store job outputs and other data
    up to several terabytes. The Depot storage is mounted at ``/depot/cms/`` and ``/home/<username>/depot/``.
    Users with Purdue account have **read/write** access, while other users have **read-only** access.
  * **Purdue EOS**: serves as a storage space for large centrally produced and private datasets,
    as well as the Grid directories of Purdue users. The Purdue EOS storage is mounted at
    ``/eos/purdue/`` and ``/home/<username>/eos-purdue/`` with **read-only** access.
    CMS datasets can be found at these locations under ``/store/data/`` and ``/store/mc/``,
    and the Grid directories of Purdue users under ``/store/user/``.
  * `CVMFS <https://cernvm.cern.ch/fs/>`_: mounted at ``/cvmfs/`` with **read-only** access.
    The main use case for CVMFS is installation of CMSSW releases.
  * `CERNBox (CERN EOS) <https://cernbox.cern.ch/>`_: regardless of the login method (Purdue/CERN/FNAL),
    any user can get **read/write** access to their CERNBox directory, if they have a CERN account.
    CERNBox is mounted at ``/eos/cern/`` and ``/home/<username>/eos-cern/``.

.. warning::
   
    Access to CERNBox is not enabled by default. To set it up, please follow these instructions:
    :doc:`guide-cern-eos`

* **Other options:**

    * **Git** functionality is enabled, users can use GitHub or GitLab to store and share their work.
      The Git extension located in the left sidebar allows to work with repositories interactively  (commit, push, pull, etc.).
    * **XRootD client** is installed and can be used to access data stored at other CERN sites.

* **Subscribing datasets with Rucio**
    :doc:`guide-rucio`
