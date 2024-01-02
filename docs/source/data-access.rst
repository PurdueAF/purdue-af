.. _data-access:

Data access, storage, file sharing
==================================

The Purdue Analysis Facility provides access to multiple storage options:

* **Local storage:** at first login, each user is given a 25GB directory at ``/home/<username>/``.
  These directories persist between sessions and can be used to store user notebooks, scripts, and other data.
  However, the directories will be deleted after 6 months of inactivity, therefore they should not be used for
  long-term storage.
* **Shared storage:** each user is also given a directory at ``/work/users/<username>/`` with 100GB quota.
  These directories are visible to all Analysis Facility users, and therefore can be used for collaboration
  and sharing your work. Project directories can be created in ``/work/`` storage by the Analysis Facility
  administrators at the request of the project's PI.
* **External storage:** multiple external storage volumes are mounted to the user pod.
  These options serve different purposes and have different access permissions.

  * `CERNBox (CERN EOS) <https://cernbox.cern.ch/>`_: regardless of the login method (Purdue/CERN/FNAL),
    any user can get **read/write** access to their CERNBox directory, if they have a CERN account.
    CERNBox is the only writable external directory accessible to non-Purdue users,
    therefore it is recommended to use it for file sharing.
    CERNBox is mounted at ``/eos/cern/`` and ``/home/<username>/eos-cern/``.
    .. warning::

       **Warning:** access to CERNBox is not enabled by default. To set it up, please follow these instructions:
       :doc:`guide-cern-eos`

  * `CVMFS <https://cernvm.cern.ch/fs/>`_: mounted at ``/cvmfs/`` with **read-only** access.
    The main use case for CVMFS is  installation of CMSSW releases.
  * **Purdue EOS**: serves as a storage space for large centrally produced and private datasets,
    as well as the Grid directories of Purdue users. The Purdue EOS storage is mounted at
    ``/eos/purdue/`` and ``/home/<username>/eos-purdue/`` with **read-only** access.
    CMS datasets can be found at these locations under ``/store/data/`` and ``/store/mc/``,
    and the user’s Grid directories under ``/store/user/``.
  * **Purdue Depot**: shared project space at Purdue, which can be used to store job outputs and other data
    up to several terabytes. The Depot storage is mounted at ``/depot/cms/`` and ``/home/<username>/depot/``.
    Purdue users have **read/write** access, while other users have **read-only** access.
    Therefore, Depot directory can be used for file sharing between Purdue users.

* **Other options:**

    * **Git** functionality is enabled, users can use GitHub or GitLab to store and share their work.
    * **XRootD client** is installed and can be used to access data stored at other CERN sites.

* **Subscribing datasets with Rucio**
    :doc:`guide-rucio`
