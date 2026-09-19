# Data access

!!! abstract "TL;DR"

    * To access remote datasets, use the `XRootD` protocol (requires a
      [VOMS proxy](getting-started.md#6-set-up-a-voms-proxy)).
    * Repeated reading of the same remote dataset is faster if you use `XCache` via 
      the following prefix: `root://xcache.cms.rcac.purdue.edu/`.
    * If a dataset is only available on tape, it needs to first be replicated
      (aka copied, "subscribed") to disk.
    * To subscribe a dataset to Purdue, create a [Rucio rule](guide-rucio.md).

## General principles

* CMS organizes its data into **Datasets**, composed of **Blocks**, which contain
  the individual **ROOT files**.

* All information about Datasets, Blocks and Files is accessed through the
  Data Aggregation System (**DAS**), either via its
  [Web](https://cmsweb.cern.ch/das/) or
  [command line](https://cmsweb.cern.ch/das/cli) interfaces.

* CMS manages its data — placement, replication, availability, etc. — through
  CERN's [Rucio](https://rucio.cern.ch/) system.

* All CMS published datasets are available to all registered users, regardless of
  their location, through the
  [Any Data, Anytime, Anywhere (AAA)](https://twiki.cern.ch/twiki/bin/view/CMSPublic/CMSXrootDArchitecture#CMS_XRootD_Architecture_and_AAA)
  service using the **XRootD** protocol.

* There is **no need to have a dataset stored at Purdue** in order to access its
  files in your analysis. Only when increased data-access performance is desired
  (e.g. when running over the same dataset many times) should you request a local
  copy by creating a [Rucio replication rule](guide-rucio.md).

* Files transferred to Purdue via [Rucio rules](guide-rucio.md) end up in the
  large **EOS** storage system, **not** in users' directories(!), and become
  accessible via the same AAA/XRootD methods as all other replicas of those
  datasets worldwide.

* Output files from your CRAB jobs also end up in EOS, in your
  [Grid directory](storage.md#saving-outputs-of-crab-jobs), and are likewise
  accessible from anywhere via AAA/XRootD.

* No CMS data file or dataset needs to reside in your `/home/` or `/work/`
  directories in order to be used in an analysis — except perhaps single, small
  files used during the development of the analysis. `/home/` directories are for
  personal files, and work directories are for developing, storing, and sharing code.

## XCache

Repeated access to remote datasets can be sped up significantly through the use of
the local **XCache** server.

* To use it, replace the XRootD redirector prefix with
  `root://xcache.cms.rcac.purdue.edu/`. For example, instead of

    ```
    root://cms-xrd-global.cern.ch//store/mc/HC/GenericTTbar/AODSIM/CMSSW_9_2_6_91X_mcRun1_realistic_v2-v2/00000/00B29645-2B76-E711-8802-FA163EB9B8B4.root
    ```

    open

    ```
    root://xcache.cms.rcac.purdue.edu//store/mc/HC/GenericTTbar/AODSIM/CMSSW_9_2_6_91X_mcRun1_realistic_v2-v2/00000/00B29645-2B76-E711-8802-FA163EB9B8B4.root
    ```

* An additional benefit of XCache is that all the XRootD redirection needed to
  locate the data is done by the XCache server itself: it finds which site has the
  file, downloads it onto its fast local disks, and serves it from there — you do
  not need to know where the file is stored.

* The one case where access through XCache does **not** work is when files are only
  available **on tape** — i.e. no CMS Tier site has the file on disk, and a tape
  recall is necessary. In that case you have to create a
  [Rucio replication rule](guide-rucio.md).

## A user's perspective

* All CMS data files available at Purdue's EOS storage are accessible (read-only)
  via a normal POSIX filesystem mount under `/eos/purdue/store/` — this is useful
  for interactive navigation, searching, and testing of analysis code.
* Analysis code should be written with the general goal of accessing data files via
  XRootD, potentially sped up via XCache.
* Datasets which *absolutely* need to be present at Purdue can be "subscribed"
  (i.e. temporarily copied) to our storage by creating a [Rucio rule](guide-rucio.md).
* Individual ROOT files can be copied to/from other sites using the `xrdcp` and
  `gfal-copy` commands. Files transferred in this way end up in your `/home/` or
  `/work/` directory, not in EOS.

## Examples

### Copy a single ROOT file from CERN / Fermilab

1. Make sure you have a valid **VOMS proxy** (check with `voms-proxy-info`); if
   not, [create a fresh one](getting-started.md#6-set-up-a-voms-proxy).

2. Use **gfal commands** to copy a single file:

    ```shell
    gfal-copy root://eoscms.cern.ch//store/group/phys_higgs/common_plots/March_2023/high_mass_MSSM/MSSM_limits_hMSSM.pdf ./
    ```

    or a whole directory, recursively:

    ```shell
    gfal-copy -r root://eoscms.cern.ch//store/group/phys_higgs/common_plots/March_2023/ ./
    ```

3. Alternatively, use **XRootD commands** to copy a file:

    ```shell
    xrdcp root://cms-xrd-global.cern.ch//store/group/phys_higgs/common_plots/March_2023/high_mass_MSSM/MSSM_limits_hMSSM.pdf ./
    ```

    or a whole directory:

    ```shell
    xrdcp -r root://eos.cms.rcac.purdue.edu//store/user/piperov/SingleMuon ./
    ```

!!! note "See also"

    * [Rucio tutorial](guide-rucio.md)
    * [Writing to EOS](guide-eos-write.md)
    * [Storage volumes](storage.md)
    * [Demo notebook: accessing files in different storage locations](https://github.com/PurdueAF/purdue-af-demos/blob/master/storage-data-access.ipynb)
