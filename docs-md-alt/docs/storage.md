# Storage volumes

## Overview

The following table summarizes size, access modes, and accessibility of each storage
volume (scroll sideways for details).

| Storage volume | Path | Size | Access mode | Mounted in Slurm jobs and Dask/Slurm workers | Mounted in Dask/k8s workers | Writable by users w/o Purdue account |
| --- | --- | --- | --- | --- | --- | --- |
| AF home storage | `/home/<username>/` | 25 GB | Read/write | ❌ | ❌ | ✅ |
| Purdue Depot storage | `/depot/cms/` | up to 1 TB | Read/write for Purdue users, read-only for others | ✅ | ✅ | ❌ |
| AF work storage | `/work/users/<username>/` | 100 GB | Read/write | ❌ | ✅ | ✅ |
| AF shared project storage | `/work/projects/` | up to 1 TB | Read/write | ❌ | ✅ | ✅ |
| Purdue EOS | `/eos/purdue/` | up to 100 TB | Read-only (writable via `gfal`/`xrdcp`) | ✅ | ✅ | ❌ |
| CVMFS | `/cvmfs/` | N/A | Read-only | ✅ | ✅ | ❌ |
| CERNBox (CERN EOS) | `/eos/cern/` | N/A | Read/write | ❌ | ❌ | ✅ |

!!! warning "Keep your home directory small"

    Your `/home/<username>/` directory (the root directory of the JupyterLab file
    browser) has a strict quota of **25 GB**. If you go over this limit, **you will
    not be able to start a session on Purdue AF**.

    Rather than storing your data, Pixi or Conda environments, etc. in your home
    directory, use the other storage volumes listed below. You can check your current
    home directory usage with:

    ```shell
    du -sh $HOME
    ```

## Which storage volume should I use?

Below are common storage use cases with recommendations on which volume to use.

### Storing analysis code and environments

* Keep your code in a **Git repository** (GitHub or GitLab), cloned into one of
  the writable volumes.
* **Purdue users:** use `/depot/cms/users/<username>/` or a group directory such as
  `/depot/cms/<group-name>/`. Any code that uses Slurm or Dask Gateway with the
  Slurm backend **must** be stored on Depot, since the other volumes are not
  mounted in Slurm jobs.
* **CERN / FNAL users:** use `/work/users/<username>/` or a shared project directory
  `/work/projects/<project-name>/`.

### Storing custom Pixi or Conda environments

* In order for Pixi or Conda environments to appear as JupyterLab kernels, they must
  be stored in publicly readable directories, so `/depot/cms/private/` directories
  will NOT work. Possible locations:

    * group directories on Depot (for example, `/depot/cms/top/`);
    * personal directories on work storage: `/work/users/<username>/`;
    * shared project directories on work storage: `/work/projects/<project-name>/`.

* If you use Slurm jobs or Dask Gateway workers, make sure that the directory where
  the environment is stored is visible from the workers (see the table above).
  In particular, **Slurm workers cannot see `/work/` storage** — environments used
  with the Slurm backend must live on Depot.

### Transferring official CMS datasets to Purdue

1. Locate the dataset using [DAS (CMS Data Aggregation System)](https://cmsweb.cern.ch/das/).
2. Use [Rucio](guide-rucio.md) to "subscribe" the dataset to Purdue for a *limited*
   amount of time.
3. The dataset will be copied to **Purdue EOS** and appear under
   `/eos/purdue/store/mc/` or `/eos/purdue/store/data/`.

See [Data access](data-access.md) for the full picture.

### Saving outputs of CRAB jobs

* The outputs of CRAB jobs are written to your Grid directory:
  `/eos/purdue/store/user/<cern-username>`.
  *Note: your CERN username is different from your Purdue username!*
* The Grid directory at Purdue EOS is created only for Purdue-affiliated users.
  This must be indicated when creating a Purdue Tier-2 account.
* If you can't see your Grid directory under `/eos/purdue/store/user/`, please
  [contact support](support.md).

### Processing ("skimming") CMS datasets

The best storage volume depends on the size of the output:

* **Large outputs (over 100 GB):** save to **Purdue EOS**. Since Purdue EOS is not
  directly writable, save outputs into `/tmp/<username>/` first and then copy them
  to EOS using `gfal` or `xrdcp` commands — see [Writing to EOS](guide-eos-write.md).
* **Small outputs (under 100 GB):**

    * Purdue users should use **Depot** (`/depot/cms/`). If the outputs need to be
      accessible by other users, use a group directory (e.g. `/depot/cms/top/`).
    * Non-Purdue users should use **work storage**: `/work/users/<username>/` or
      `/work/projects/<project-name>/`.

!!! warning "Don't hammer Depot from many jobs at once"

    Avoid writing many files to Depot at the same time, as it may slow Depot down
    for everyone. If your jobs produce large outputs, first save them into
    `/tmp/<username>/` on the individual Slurm jobs / Dask workers, and then copy
    them over to EOS using `gfal` or `xrdcp` commands — see
    [Data access](data-access.md).

## Shared project directories

Directories under `/work/projects/` are intended for groups of users collaborating
on the same analysis, regardless of which institution their accounts belong to.
If you would like a project directory to be created, [contact support](support.md).

## Other options

* **Git**: users can use GitHub or GitLab to store and share their work. The Git
  extension located in the left sidebar allows you to work with repositories
  interactively (commit, push, pull, etc.).
* **CERNBox**: anyone with a CERN account can mount their CERNBox directory —
  see [CERNBox access](guide-cern-eos.md).
* **XRootD client** is installed and can be used to access data stored at other
  CMS sites — see [Data access](data-access.md).
