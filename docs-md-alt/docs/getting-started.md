# Getting started

Welcome to the Purdue Analysis Facility!

This guide will help you to quickly set up the work environment for your analysis.
It should take about 15 minutes to go through all the steps; after that, you will
have a fully functional session with access to CMS data and analysis software.

[🚀 Login to Purdue Analysis Facility](https://cms.geddes.rcac.purdue.edu/hub){ target="_blank" }

## 1. Choose a login method

Three login methods are supported:

* **Purdue University account** — recommended if you are a Purdue-affiliated user,
  since it unlocks Slurm submission and write access to Depot storage. External collaborators working with Purdue research groups can also request a guest computing account.
* **CERN account** (CMS users only)
* **FNAL account**

Note that the same person logging in with different credentials is treated as
different users, with separate home directories and storage allocations.
See [Login methods and usernames](login-methods.md) for details.

## 2. Select resources

After a successful login, you will be redirected to a page where you can select
the number of CPU cores (up to 128), the amount of RAM (up to 128 GB), and
(optionally) a GPU for your session. You can also choose which web interface the
session starts with: **JupyterLab** (default) or **VS Code (code-server)**.

The default values are enough to get started. If you need more resources later,
shut down the session (`File → Hub Control Panel → Stop My Server`, or the
`Shut Down` button in the top right corner) and recreate it with a different selection.

!!! important "GPU selection"

    There are two options for GPU selection:

    * **5 GB "slice"** of an Nvidia A100 GPU — almost always available, sufficient
      for inference and small-scale training;
    * **Full 40 GB instance** of an Nvidia A100 GPU — more powerful, but subject
      to availability.

    The resource selection form shows **live availability** next to each GPU option.

    [Learn more about GPU access at Purdue AF](gpus.md)

!!! tip

    If for any reason the session creation fails but you need urgent access to your
    files, use the `Minimal JupyterLab interface` option.

## 3. Review storage volumes

After the session has started, take a moment to understand the available storage:

* The default directory in the file browser and Terminal is `/home/<username>`.
  It has a **strict 25 GB quota** — exceeding it will prevent your session from starting,
  so keep your data, environments, and large outputs elsewhere.
* In the file browser you will see symlinks to the other storage volumes:

    * `work` (mounted at `/work/`) — shared storage for all AF users.
      There are 100 GB personal directories under `/work/users/`, and project
      directories under `/work/projects/`.
    * `depot` (mounted at `/depot/cms`) — shared storage, **writable only for Purdue
      users**. Code and environments used by Slurm jobs must live here.
    * Purdue EOS storage (mounted at `/eos/purdue`) — **read-only** view of the   
      Purdue Tier-2 storage, which holds large CMS datasets and users' Grid directories.

!!! note "See also"

    * Detailed description of all storage options: [Storage volumes](storage.md)
    * [CERNBox access](guide-cern-eos.md)

## 4. Review kernels and software environments

The analysis software at Purdue AF is managed via Pixi and Conda environments and
Jupyter kernels.

To get started, you can use the **global Pixi environment**, which contains all
common HEP analysis packages and ML libraries. It is located at `/work/pixi/global/`
and available as the default Jupyter kernel across the facility. To use the environment in Terminal, run the following commands:

```shell
cd /work/pixi/global/
pixi shell
cd /your/working/directory/
```

For your own analyses, we recommend creating project-specific Pixi environments —
see the [Pixi guide](guide-pixi.md).

See [Software stacks](software.md) for a complete overview. If you need a package
that is missing from the pre-installed kernels, please
[contact Purdue AF support](support.md).

## 5. Set up GitHub access

Follow these instructions:

* [Generating a new SSH key and adding it to the ssh-agent](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/generating-a-new-ssh-key-and-adding-it-to-the-ssh-agent){ target="_blank" }
* [Adding a new SSH key to your GitHub account](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/adding-a-new-ssh-key-to-your-github-account){ target="_blank" }

After you have generated an SSH key and added it to your GitHub account, run the
following command in a Terminal to confirm that GitHub authentication was successful:

```shell
ssh -T git@github.com
```

## 6. Set up a VOMS proxy

A VOMS proxy is required to access CMS data via XRootD, submit CRAB jobs, and use Rucio.

1. If you don't have a CMS VOMS certificate yet, obtain and install one following
   the instructions at the
   [CMS TWiki](https://twiki.cern.ch/twiki/bin/view/CMSPublic/WorkBookStartingGrid),
   specifically the section **"Obtaining and installing your Certificate"**.

    ??? note "Uploading the certificate files to Purdue AF"

        To upload files (e.g. `usercert.pem` / `userkey.pem`) to Purdue AF, you can either:

        - drag-and-drop a file from a local directory into the Jupyter file browser, or
        - click the "upload" icon (upward arrow) at the top of the Jupyter file browser
          and select a file to upload.

2. (Optional) Specify the path where your VOMS proxy will be stored. If you are
   using Slurm or Dask Gateway with the Slurm backend, the proxy location must be
   on Depot (currently only possible for users with a Purdue account):

    ```shell
    export X509_USER_PROXY=/depot/cms/users/$USER/x509up_u$NB_UID
    ```

3. Activate the VOMS proxy:

    ```shell
    voms-proxy-init --rfc --voms cms -valid 192:00
    ```

## 7. Join user support channels

* [Subscribe to the mailing list (Purdue users only)](support.md)
* Join the [Purdue AF support channel on CERN Mattermost](https://mattermost.web.cern.ch/cms-exp/channels/purdue-analysis-facility)
  (CERN login required)

## Next steps

* Learn the [JupyterLab interface and other ways to work at Purdue AF](interface.md)
* Set up a [project-specific Pixi environment](guide-pixi.md)
* Try the [interactive demos](https://github.com/PurdueAF/purdue-af-demos)
* When your analysis outgrows a single session, [scale out with Dask Gateway](guide-dask-gateway.md)
* Connect an AI agent to your session via the [agentic interface](guide-agentic-interface.md)
