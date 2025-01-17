.. attention::

    **Updated: January 17, 2025**

    The Purdue EOS storage servers are down since December 23, 2024.

    All EOS directories are affected, including ``/store/user``, ``/store/data``, and ``/store/mc``.
    
    CMS datasets can still be accessed from other CMS sites via XRootD or XCache.
    Datasets with the only copy at T2_Purdue, as well as any private data at EOS
    are not accessible at the moment; we hope to recover all data by January 19th.

    Outputs of CRAB jobs executed since December 23rd are stored on fallback servers,
    please contact the site support if you need to access them.

    All other storage (``/home/``, ``/work/``, Depot) remains fully operational.

    We will keep you updated on the progress of the recovery.

Purdue Analysis Facility
======================================

The Purdue Analysis Facility is designed to provide an interactive environment
for fast and scalable CMS physics analyses using dedicated computing resources at Purdue.

|

|login_to_af|

|

.. |login_to_af| raw:: html

    <div style="text-align: center">
        <a href="https://cms.geddes.rcac.purdue.edu/hub" target="_blank">
            🚀 Login to Purdue Analysis Facility
        </a>
    </div>

.. admonition:: Supported login credentials:

   * Purdue University account (BoilerKey)
   * CERN account (CMS users only)
   * FNAL account

|

Purdue AF features a JupyterLab interface with access to CPUs, GPUs, and
distributed computing resources via SLURM and Dask Gateway.
It features a software stack suitable for all steps of
HEP analysis design and execution, including popular tools such as
``coffea``, ``ROOT``, ``RDataFrame``, as well as popular
machine learning packages like ``tensorflow``. 
Users are provided with a variety of data access methods
(``XRootD``, ``XCache``, ``Rucio``), as well as multiple
private and shared storage volumes.

The software and functionality is regularly updated to provide state-of-the-art
tools and features for fast, efficient, collaborative HEP research.

.. toctree::
    :caption: Documentation
    :maxdepth: 1

    doc-getting-started
    doc-interface
    doc-storage
    doc-software
    doc-gpus
    doc-data-access
    doc-hardware
    doc-support
    doc-contributing

    .. doc-login-methods
    .. doc-interface

.. toctree::
    :hidden:
    :caption: Monitoring

    Monitoring dashboard <https://cms.geddes.rcac.purdue.edu/grafana/d/purdue-af-dashboard/purdue-analysis-facility-dashboard>


.. toctree::
    :hidden:
    :caption: User guides
    :maxdepth: 1

    guide-conda
    guide-upload
    guide-ssh-access
    guide-vscode
    guide-dask
    guide-dask-gateway
    guide-rucio
    guide-cern-eos
    guide-binderhub
    guide-mc-gen
    guide-demos

.. toctree::
    :hidden:
    :caption: Useful links

    Purdue Tier-2 website <https://www.physics.purdue.edu/Tier2/>
    CMS Common Analysis Tools documentation <https://cms-analysis.docs.cern.ch>
    Data Aggregation System (DAS) <https://cmsweb.cern.ch/das/>

