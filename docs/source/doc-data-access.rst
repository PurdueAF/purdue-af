Data access 
============================

.. admonition:: TL;DR

  - To access remote datasets, use ``XRootD`` protocol.
  - Repeated reading of the same remote dataset is faster if you use ``XCache``.
  - To enable ``XCache``, replace ``XRootD`` prefix in the dataset path with ``xcache.cms.rcac.purdue.edu``.
  - If a dataset is only available on tape, it needs to first be replicated (aka copied, subscribed) to disk.
  - To subscribe a dataset to Purdue, create a `Rucio rule` with a limited lifetime.

General Principles
--------------------

- CMS organizes its data into **Datasets**, composed of **Blocks**,
  which contain the individual **ROOT files**. 

- All information about Datasets, Blocks and Files is accessed through the
  Data Aggregation System (**DAS**) either via its `Web <https://cmsweb.cern.ch/das/request?instance=prod/global&input=block+dataset%3D%2FDYJetsToLL_M-105To160_VBFFilter_TuneCP5_PSweights_13TeV-amcatnloFXFX-pythia8%2FRunIIFall18wmLHEGS-VBFPostMGFilter_102X_upgrade2018_realistic_v11_ext1-v1%2FGEN-SIM>`_
  or `CommandLine <https://cmsweb.cern.ch/das/cli>`_ interfaces.

- CMS manages its data - placement, replication, availability etc - through
  CERN’s `Rucio <https://rucio.cern.ch/>`_ system. 

- All CMS published datasets are available to all registered users, regardless
  of their location, through the `Any Data, Anytime, Anywhere (AAA) <https://twiki.cern.ch/twiki/bin/view/CMSPublic/CMSXrootDArchitecture#CMS_XRootD_Architecture_and_AAA>`_
  service using the **XRootD** protocol.

- Repeated access to remote datasets can be sped up significantly through the
  use of the local **XCache** server. 

  - Additional benefit of using the local **XCache** server is that all the XRootD redirection needed for locating the data is done by that server, so the user does not need to do it explicitly. I.e. when the user tries to open file  ``/store/mc/HC/GenericTTbar/AODSIM/CMSSW_9_2_6_91X_mcRun1_realistic_v2-v2/00000/00B29645-2B76-E711-8802-FA163EB9B8B4.root`` they can just ask for ``root://xcache.cms.rcac.purdue.edu//store/mc/HC/GenericTTbar/AODSIM/CMSSW_9_2_6_91X_mcRun1_realistic_v2-v2/00000/00B29645-2B76-E711-8802-FA163EB9B8B4.root`` and the **XCache** server will find what site has this file, download it into its fast local disks, and then serve it from there to the user. 

  - The one exception when access through **XCache** does **not** work is if files are only available on tape. That is - no CMS Tier site has the file on disk, and therefore a tape recall is necessary. In that case the user has to create a Rucio replication rue, as explained below.

- There is no need to have a given dataset at the site in order to access its
  files in your analysis. Only when increased data-access performance is desired
  (e.g. when running a given analysis multiple times at the site)
  the user can request a local copy to be created at the site by creating
  `Rucio replication rules <https://twiki.cern.ch/twiki/bin/viewauth/CMS/Rucio>`_. 

- Files transferred to our site via Rucio rules end up stored in the large EOS
  storage system at Purdue, **not** in users' directories(!), and become accessible
  via the same AAA/XRootD methods as all other replicas of those datasets worldwide. 

- Rucio rules are created with **finite lifetime** (weeks, months) and need to
  be **approved** by the Site Admins (otherwise the site's storage system may get
  overfilled).

- Output files from user's CRAB jobs also end up in the large EOS storage system,
  in the "user" section (``/store/user/<username>``), and are also accessible
  from anywhere via AAA/XRootD.

- No CMS datafile/dataset needs to reside in the user's ``/home/`` or ``/work/`` 
  directories in order to be used in an analysis, except perhaps for single,
  small files used initially during the development of the analysis.
  ``/home/`` directories are for user's personal files, and work-directories -
  for developing, storing and sharing code.

Users' Perspective
-------------------

- All CMS datafiles available at Purdue's EOS storage are accessible (read-only)
  via normal UNIX/POSIX filesystem mount under ``/eos/purdue/store/`` -
  this is useful for interactive navigation, search and testing of analysis code.

- Analysis code is written with the general goal of accessing datafiles via
  XRootD, potentially sped up via XCache.

- Datasets which `absolutely` need to be present at Purdue can be "subscribed"
  (i.e. temporarily copied) to our Storage by creating a Rucio rule.

- There are limits on how much data a user can keep at the site, and for how long. 

- Files transferred via Rucio end up in EOS, not in users' directories.

- Individual ROOT files can be copied to/from other sites using the ``xrdcp``
  and ``gfal-copy`` commands. Files transferred in this way end up in the
  user's ``/home/`` or ``/work/`` directory, not in EOS.


Examples
-----------

- Copy a single ROOT file from CERN/Fermilab.

  - Make sure you have a valid **VOMS proxy**:
  
    .. code-block:: shell

       $ voms-proxy-info
       ...
       type      : RFC3820 compliant impersonation proxy
       strength  : 2048
       timeleft  : 191:59:52

  - If not - create a fresh one:

    .. code-block:: shell

       $ voms-proxy-info
       Proxy not found: 

       $ voms-proxy-init -voms cms -rfc -valid 192:00
       Enter GRID pass phrase for this identity:


  - Use **gfal commands** to copy single file:

    .. code-block:: shell

       $ gfal-copy root://eoscms.cern.ch//store/group/phys_higgs/common_plots/March_2023/high_mass_MSSM/MSSM_limits_hMSSM.pdf ./

  - Or a whole directory, recursively:

    .. code-block:: shell

       $ gfal-copy -r root://eoscms.cern.ch//store/group/phys_higgs/common_plots/March_2023/ ./


  - Alternatively - use **xrootd commands** to copy a file:

    .. code-block:: shell

       $ xrdcp root://cms-xrd-global.cern.ch//store/group/phys_higgs/common_plots/March_2023/high_mass_MSSM/MSSM_limits_hMSSM.pdf ./

  - Or a whole directory:

    .. code-block:: shell

       $ xrdcp -r root://eos.cms.rcac.purdue.edu//store/user/piperov/SingleMuon ./


- Create replication rule in Rucio for a dataset/block

  - Make sure your Rucio environment is `setup <https://twiki.cern.ch/twiki/bin/viewauth/CMS/Rucio>`_:

    .. code-block:: shell

       $ rucio whoami
       ...
       status     : ACTIVE
       account_type : USER

  - Then create a replication rule for the dataset you want to have at
    T2_US_Purdue for the next 3 months (7776000 sec.):

    .. code-block:: shell

       $ rucio add-rule --lifetime 7776000 --ask-approval cms:/DYJetsToLL_M-105To160_VBFFilter_TuneCP5_PSweights_13TeV-amcatnloFXFX-pythia8/RunIIFall18wmLHEGS-VBFPostMGFilter_102X_upgrade2018_realistic_v11_ext1-v1/GEN-SIM 1 T2_US_Purdue

       (take a note of the hash printed as result - that is the number by which you identify your new rule)

  - Or, if you don't need the whole Dataset, but just one Block of files:

    .. code-block:: shell

       $ rucio add-rule --lifetime 7776000 --ask-approval cms:/TTJets_TuneCP5_13TeV-amcatnloFXFX-pythia8/RunIISummer20UL17RECO-106X_mc2017_realistic_v6-v2/AODSIM#28298d51-0804-40b1-b49b-54482450c221 1 T2_US_Purdue


  - List your Rucio replication rules:

    .. code-block:: shell

       $ rucio list-rules --account <your_username>


