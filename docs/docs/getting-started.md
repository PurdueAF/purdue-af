# Getting started

Welcome to the Purdue Analysis Facility!

This guide will help you to quickly set up the work environment for your analysis.
It should take about 15 minutes to go through all the steps; after that, you will
have a fully functional session with access to CMS data and analysis software.

[🚀 Login to Purdue Analysis Facility](https://cms.geddes.rcac.purdue.edu/hub){ target="_blank" }

## 1. Choose a login method

Choose one of the [supported login methods](login-methods.md) and keep using
it. A Purdue University account is recommended if you are a Purdue-affiliated
user, since it unlocks the [features available only to Purdue accounts](login-methods.md#account-permissions-at-a-glance).
External collaborators working with Purdue research groups can also request a
guest computing account.

## 2. Select resources

After a successful login, you will be redirected to a page where you can select
the number of CPU cores, the amount of RAM
([what the selection means](scaling-out.md#session-resources)), and (optionally)
a [GPU](gpus.md) for your session. You can also choose which web interface the
session starts with: **JupyterLab** (default) or **VS Code (code-server)**.

The default values are enough to get started. If you need more resources later,
shut down the session (`File → Hub Control Panel → Stop My Server`, or the
`Shut Down` button in the top right corner) and recreate it with a different selection.

If the session fails to start, see
[Troubleshooting](troubleshooting.md#sessions).

## 3. Review storage volumes

After the session has started, take a moment to understand the available storage.
The default directory in the file browser and Terminal is your home directory,
`/home/<username>`. It is small, and going over its
[quota](storage.md#quotas) prevents your session from starting — keep your data,
environments, and large outputs on the other volumes described in
[Storage volumes](storage.md).

## 4. Review kernels and software environments

The analysis software at Purdue AF is managed via Pixi and Conda environments and
Jupyter kernels.

To get started, you can use the **global Pixi environment**, which contains all
common HEP analysis packages and ML libraries. It is located at `/work/pixi/global/`
and has its own [Jupyter kernel](software.md#jupyter-kernels). To use the environment in Terminal, run the following commands:

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
   To upload the certificate files (`usercert.pem` / `userkey.pem`) to Purdue AF,
   see [Uploading and downloading files](guide-file-transfer.md).

2. (Optional) Specify the path where your VOMS proxy will be stored. Dask
   Gateway workers and Slurm jobs can only read a proxy stored on a volume they
   mount — see [Reading data via XRootD](guide-dask-gateway.md#environment-variables).

3. Activate the VOMS proxy:

    ```shell
    voms-proxy-init --rfc --voms cms -valid 192:00
    ```

## 7. Join user support channels

Join the Mattermost channel and the mailing list listed in [Support](support.md).

## Next steps

* Learn the [JupyterLab interface and other ways to work at Purdue AF](interface.md)
* Set up a [project-specific Pixi environment](guide-pixi.md)
* Try the [interactive demos](https://github.com/PurdueAF/purdue-af-demos)
* When your analysis outgrows a single session, [scale out with Dask Gateway](guide-dask-gateway.md)
* Run `claude` or `codex` in a terminal — both come pre-connected to the
  [agentic interface](guide-agentic-interface.md), or connect your own agent
