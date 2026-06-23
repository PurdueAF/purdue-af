# Rucio tutorial

[Rucio](https://rucio.cern.ch/) is the system CMS uses to manage its data —
placement, replication, availability, etc. At Purdue AF, the Rucio client is
pre-installed; users can create Rucio rules following the standard
[procedures](https://twiki.cern.ch/twiki/bin/view/CMSPublic/RucioUserDocsRules).

The most common use case is "subscribing" a dataset to Purdue, i.e. requesting a
temporary local copy of a dataset at the `T2_US_Purdue` site — see
[Data access](data-access.md) for when and why you would want to do this.

## Initial setup

Execute the following commands in a Terminal:

```shell
source /cvmfs/cms.cern.ch/cmsset_default.sh
source /cvmfs/cms.cern.ch/rucio/setup-py3.sh
voms-proxy-init -voms cms -rfc -valid 192:00
export RUCIO_ACCOUNT=<your-CERN-account>
```

Check that Rucio recognizes you:

```shell
$ rucio whoami
...
status     : ACTIVE
account_type : USER
```

## Working with Rucio rules

* List your existing Rucio rules:

    ```shell
    rucio list-rules --account <your-CERN-account>
    ```

* Create a replication rule for a dataset (here: a 3-month lifetime, i.e.
  7776000 seconds):

    ```shell
    rucio add-rule --lifetime 7776000 --ask-approval cms:/DYJetsToLL_M-105To160_VBFFilter_TuneCP5_PSweights_13TeV-amcatnloFXFX-pythia8/RunIIFall18wmLHEGS-VBFPostMGFilter_102X_upgrade2018_realistic_v11_ext1-v1/GEN-SIM 1 T2_US_Purdue
    ```

    Take note of the hash printed as a result — that is the ID by which you can
    identify your new rule.

* The requests follow the usual approval workflow; after completion, the datasets
  will be available in the Purdue EOS storage, under `/eos/purdue/store/mc/` or
  `/eos/purdue/store/data/`.

!!! note

    Rucio rules are created with a **finite lifetime** (weeks, months) and need to
    be **approved** by the site admins — otherwise the site's storage system would
    overfill. There are limits on how much data a user can keep at the site, and
    for how long.

!!! note "See also"

    * [Data access](data-access.md) — the full picture: XRootD, XCache, DAS, Rucio
    * [Storage volumes](storage.md)
