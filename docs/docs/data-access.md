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

* There is **no need to have a dataset stored at Purdue** in order to read its
  files in your analysis. Request a local copy with a
  [Rucio replication rule](guide-rucio.md) only when you read the same dataset
  many times and need the speed, or when it exists only on tape.

* Files replicated to Purdue by Rucio rules, and the outputs of your CRAB jobs
  (in your [Grid directory](storage.md#saving-outputs-of-crab-jobs)), are stored
  in Purdue **EOS**, not in users' directories. They are read over XRootD like
  any other replica, and can be browsed read-only under `/eos/purdue/store/`.

* Analysis code reads data over XRootD, best through XCache, rather than from
  copies in `/home/` or `/work/`; copy single small files there only while
  developing.

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

## Examples

### Copy a file from another site

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

    * [Rucio](guide-rucio.md)
    * [Writing to EOS](guide-eos-write.md)
    * [Storage volumes](storage.md)
