Purdue Analysis Facility documentation
======================================

The Purdue Analysis Facility is designed to provide an interactive environment
for fast and scalable CMS physics analyses using dedicated computing resources at Purdue.

`🚀 Login to Purdue Analysis Facility <https://cms.geddes.rcac.purdue.edu>`_


.. note::

   Supported login options:

   * Purdue University account (BoilerKey)
   * CERN account (CMS users only)
   * FNAL account

.. The same person using different accounts to sign in will be treated as different users.

.. Each user is provided with a 25GB private ``/home/`` directory at first login,
.. as well as 100GB storage space in a shared ``/work/`` directory.
.. These directories will persist between sessions, but will be deleted after 6 months of inactivity.

.. toctree::
    :caption: Documentation
    :maxdepth: 1

    doc-login-options
    doc-interface
    doc-storage
    doc-software
    doc-hardware
    doc-contacts

.. toctree::
    :caption: Monitoring
    :maxdepth: 1

    monitoring

.. toctree::
    :caption: User guides
    :maxdepth: 1

    guide-cern-eos
    guide-conda
    guide-dask-gateway
    guide-rucio
