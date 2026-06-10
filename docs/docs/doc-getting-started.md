# Getting started

!!! note "Join the Purdue AF support channel on CERN Mattermost"

    [https://mattermost.web.cern.ch/cms-exp/channels/purdue-analysis-facility](https://mattermost.web.cern.ch/cms-exp/channels/purdue-analysis-facility)
    (CERN login required)

Welcome to the Purdue Analysis Facility!

This guide will help you to quickly set up the work environment for your analysis.

[🚀 Login to Purdue Analysis Facility](https://cms.geddes.rcac.purdue.edu/hub){ target="_blank" }

## 1. Choose a login method

* Purdue University account - recommended if you are a Purdue user
* CERN account (CMS users only)
* FNAL account

## 2. Select resources

After a successful login, you will be redirected to a page
where you can select the number of CPU cores, RAM, and GPUs for your session.

The default values are enough to get started; if more resources are needed,
you can close the session (`Shut Down` button in top right corner) and
recreate it with a different selection.

!!! important

    There are two options for GPU selection:

    * 5GB "slice" of Nvidia A100 GPU - immediately available, but less powerful
    * Full 40GB instance of Nvidia A100 GPU - more powerful, but subject to availability

    [Learn more about GPU access at Purdue AF](doc-gpus.md)

!!! tip

    If for any reason the session creation fails but you need urgent access to your files,
    use `Minimal JupyterLab interface` option.

## 3. Review storage volumes

After the session has started, review the available storage options:

* The default directory in file browser and Terminal is `/home/<username>`, it has 25 GB quota.
* In the file browser you will see symlinks to the following directories:

    * `work` (also mounted at `/work/`) - shared storage for AF users.

        There are 100GB personal directories under `work/users`, and project directories under `work/projects`.
    * `depot` (also mounted at `/depot/cms`) - shared storage **only for Purdue users**.

        Any code that uses SLURM or Dask Gateway should be stored here.
    * `eos-purdue` (also mounted at `/eos/purdue`) - **read-only** directory that stores large datasets and users'
      Grid directories.

!!! note "See also"

    * Detailed description of storage options: [Storage volumes](doc-storage.md).
    * [CERNBox access](guide-cern-eos.md)

## 4. Review kernels and Pixi/Conda environments

The analysis software at Purdue AF is managed via Pixi and Conda environments,
Jupyter kernels, as well as LCG stacks and Apptainer/Singularity images available via CVMFS.

To get started, you can use the "global" Pixi environment which contains all
common HEP analysis packages and ML libraries. It is located at `/work/pixi/global/`
and available as a Jupyter kernel across the facility.

See [Software stacks](doc-software.md) for more details.

If you need a package that is missing from the pre-installed kernels, please
[contact Purdue AF support](doc-support.md).

## 5. Set up GitHub access

Follow these instructions:

* [Generating a new SSH key and adding it to the ssh-agent](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/generating-a-new-ssh-key-and-adding-it-to-the-ssh-agent){ target="_blank" }
* [Adding a new SSH key to your GitHub account](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/adding-a-new-ssh-key-to-your-github-account){ target="_blank" }

After you have generated an SSH key and added it to your GitHub account, run the
following command in a Terminal to finish GitHub authentication:

```shell
ssh -T git@github.com
```

## 6. Set up VOMS proxy

1. In order to access data via XRootD, you will need a VOMS certificate.
   To obtain and install your CMS VOMS certificate, follow the instructions at
   [CMS TWiki](https://twiki.cern.ch/twiki/bin/view/CMSPublic/WorkBookStartingGrid),
   specifically the section **"Obtaining and installing your Certificate"**.

    ??? note "Uploading files to Purdue AF"

        To upload files to Purdue AF, you can either:

        - Drag-and-drop a file from local directory into the Jupyter file browser, OR
        - click "upload" icon (upward arrow) at the top of the Jupyter file browser
          and select a file to upload.

2. (Optional) Specify the path where your VOMS proxy will be stored. If you are
   using SLURM or Dask Gateway, the proxy location must be on Depot
   (currently only allowed for users with Purdue account):

    ```shell
    export X509_USER_PROXY=/depot/cms/users/$USER/x509up_u$NB_UID
    ```

3. Activate the VOMS proxy:

    ```
    voms-proxy-init --rfc --voms cms -valid 192:00
    ```

## 7. Join user support channels

* [Subsrcibe to the mailing list (Purdue users only)](doc-support.md)
* Join the [Purdue AF support channel on CERN Mattermost](https://mattermost.web.cern.ch/cms-exp/channels/purdue-analysis-facility)
  (CERN login required)
