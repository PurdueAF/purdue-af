Purdue Analysis Facility documentation
======================================

The Purdue Analysis Facility is designed to provide an interactive environment for fast and scalable CMS physics analyses using dedicated computing resources at Purdue.

The following login options are supported:

* Purdue University account (BoilerKey)
* CERN account (CMS users only)
* FNAL account

The same person using different accounts to sign in will be treated as different users.

Each user is provided with a 25GB private ``/home/`` directory at first login, as well as 100GB storage space in a shared ``/work/`` directory. These directories will persist between sessions, but will be deleted after 6 months of inactivity.

.. toctree::
   :maxdepth: 1
   :caption: Documentation

    login-options
    user-interface
    Data access, storage, file sharing
    Pre-installed software (Jupyter kernels)
    Hardware
    Diagrams
    Contacts

.. toctree::
   :maxdepth: 1
   :hidden:
   :caption: Monitoring

   Grafana dashboard

.. toctree::
   :maxdepth: 1
   :hidden:
   :caption: User guides

    Interactive demos
    How to enable access to CERN EOS
    How to create and share Conda environments and Jupyter kernels
    How to scale up using Dask Gateway
    Rucio tutorial

.. toctree::
   :maxdepth: 1
   :hidden:
   :caption: Contacts

   Contacts
