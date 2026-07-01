# Troubleshooting

This page collects the most common issues reported by Purdue AF users, with
solutions. If your problem is not listed here, please
[contact support](support.md).

## Sessions

??? failure "My session fails to start"

    Most commonly this happens due to issues and outages of computing infrastructure - please alert facility admins.

    Another possible cause is an **overfilled home directory**: the
    `/home/<username>/` volume has a strict 25 GB quota, and sessions cannot start
    if you are over it.

    1. Start a session with the `Minimal JupyterLab interface` option — it should
       work even when a normal session does not.
    2. Check your home directory usage: `du -sh $HOME`.
    3. Move large files (data, Pixi/Conda environments) to `/work/` or `/depot/`
       storage — see [Storage volumes](storage.md).

    If you selected a **full 40 GB A100 GPU**, the session may also fail to start
    simply because all full GPU instances are taken. Try a 5 GB slice instead, or
    start a CPU-only session.

??? failure "My session is very slow"

    * You might be trying to use custom Pixi or Conda environments on a slow 
      filesystem. Try moving them to `/work/` storage.
    * Check whether you are running out of RAM: the resources selected at session
      creation are hard limits. Restart the session with more RAM if needed.
    * Reading many small files from `/depot/` or `/eos/` can be slow — see
      [Data access](data-access.md) for faster access patterns (XCache).

??? failure "My session was shut down on its own"

    Sessions that remain **inactive for 14 days** are automatically shut down to
    release resources. Sessions holding a **full A100 GPU (40GB)** are shut down
    after only **24 hours** of inactivity, since these GPUs are scarce and shared.
    Your storage volumes are unaffected — simply start a new
    session. Sessions may also occasionally get shut down due to unplanned outages,
    so save your work regularly and keep important code in sync with a Git repository.

??? failure "I deleted/broke my configuration and want a clean start"

    Shut down your session (`File → Hub Control Panel → Stop My Server`), then
    start a new one. The session image is recreated from scratch every time; only
    the contents of your storage volumes persist.

## Storage

??? failure "I can't write to /depot/cms/"

    Depot is writable **only for users with Purdue accounts**. CERN and FNAL users
    have read-only access — use `/work/users/<username>/` or a
    `/work/projects/` directory instead. See [Storage volumes](storage.md).

    Purdue users can write to their own private directories, as well as into group dircetories to which they have access. If you don't have access to your group's directory, please contact facility admins.

??? failure "I can't write to /eos/purdue/"

    Purdue EOS is mounted read-only. To write to it, use `xrdcp` or `gfal`
    commands — see [Writing to EOS](guide-eos-write.md).

??? failure "I don't see my Grid directory under /eos/purdue/store/user/"

    The Grid directory at Purdue EOS is created only for Purdue-affiliated users,
    and must be requested when creating a Purdue Tier-2 account. If you believe you
    should have one, [contact support](support.md).

??? failure "The `eos-cern` symlink shows up as a file, not a directory"

    Restart the session (`File → Hub Control Panel → Stop My Server`), then run
    the `eos-connect` command again — see [CERNBox access](guide-cern-eos.md).

## Software and kernels

??? failure "`pixi shell` / `pixi install` fails in my home directory"

    This is intentional: Pixi projects must be located outside of `/home/` to
    avoid overflowing the 25 GB home quota. Move the project to `/work/` or
    `/depot/` — see [Pixi storage locations](guide-pixi.md#storage-locations).

??? failure "My Pixi environment doesn't show up in the project-aware kernel"

    The environment must have the `ipykernel` package installed:

    ```shell
    pixi add ipykernel
    ```

    Also make sure that the notebook is located in (a subdirectory of) the Pixi
    project directory.

??? failure "My Conda environment doesn't show up as a Jupyter kernel"

    * The environment must have the `ipykernel` package installed.
    * Kernel discovery takes 1–2 minutes after the package is installed.
    * The environment must be stored in a publicly readable directory — private
      Depot directories will not work. See [Storage volumes](storage.md).

??? failure "A package is missing from the global environment"

    [Contact support](support.md) — we regularly update the global Pixi environment.
    Alternatively, create your own [Pixi environment](guide-pixi.md) with the
    packages you need.

## Data access

??? failure "XRootD reads fail with authentication errors"

    Your VOMS proxy is probably missing or expired. Check with `voms-proxy-info`,
    and create a fresh proxy if needed:

    ```shell
    voms-proxy-init --rfc --voms cms -valid 192:00
    ```

??? failure "A dataset I need is not accessible / only on tape"

    If no CMS site has the files on disk, a tape recall is necessary: create a
    Rucio replication rule to subscribe the dataset to Purdue — see
    [Rucio tutorial](guide-rucio.md).

## Dask Gateway

??? failure "Cluster creation times out"

    Cluster creation fails if the scheduler doesn't start within 3 minutes
    (Kubernetes backend) or 10 minutes (Slurm backend). This sometimes happens
    due to resource contention — simply try resubmitting the cluster.

??? failure "I can't create a cluster: \"You may only have 1 active Dask Gateway cluster(s)\""

    Each user can have at most **one active cluster per gateway** at a time.
    Shut down your existing cluster (see
    [Shutting down clusters](guide-dask-gateway.md#5-shutting-down-clusters)),
    or wait for it to finish stopping, then try again.

??? failure "Workers fail to start or crash immediately"

    * Check that the Pixi/Conda environment passed to `new_cluster()` is **visible
      to the workers**: Slurm workers can only see `/depot/`; Kubernetes workers
      can see `/depot/` and `/work/`. See the
      [storage access table](guide-dask-gateway.md#2-shared-environments-and-storage-volumes).
    * CERN/FNAL users: make sure the `env` dictionary contains `NB_UID` and
      `NB_GID` (passing `env = dict(os.environ)` is sufficient).

??? failure "My cluster disappeared"

    Idle clusters (no connected clients — e.g. after the notebook that created the
    cluster is terminated) are shut down automatically: after **1 hour** on the
    Kubernetes backend, and after **24 hours** on the Slurm backend. Slurm workers
    are additionally limited by a **4-hour** Slurm job walltime.

??? failure "Workers can't read my data via XRootD"

    Pass the VOMS proxy location to the workers, and make sure the proxy file
    itself is on a volume the workers can read (e.g. Depot for Slurm workers):

    ```python
    os.environ["X509_USER_PROXY"] = "/depot/cms/users/<username>/x509up_..."
    cluster = gateway.new_cluster(..., env=dict(os.environ))
    ```

## SSH and IDE connections


??? failure "Remote-SSH connection from VSCode/Cursor fails"

    See the [troubleshooting section of the IDE connection guide](guide-ide-connection.md#troubleshooting).
    The usual suspects: home directory permissions (`chmod 755 ~/`), a missing or
    not-on-PATH `websocat` binary, or an expired JupyterHub token.


## Still stuck?

Send us a message — see [Support](support.md). Please include your username, the
login method you used (Purdue / CERN / FNAL), and the approximate time when the
problem occurred.
