# Troubleshooting

This page collects the most common issues reported by Purdue AF users, with
solutions. If your problem is not listed here, please
[contact support](support.md).

## Sessions

### My session fails to start

Most commonly this happens due to issues and outages of computing infrastructure - please alert facility admins.

Another possible cause is an **overfilled home directory**: sessions cannot
start while you are over the
[home directory quota](storage.md#quotas).

1. Start a session with the `Minimal JupyterLab interface` option — it should
   work even when a normal session does not.
2. [Check your home directory usage](storage.md#quotas).
3. Move large files (data, Pixi/Conda environments) to `/work/` or `/depot/`
   storage — see [Storage volumes](storage.md).

If you selected a **GPU**, the session is refused while all GPUs of that kind
are in use by other sessions; the selection form shows how many are free.
Choose another kind of GPU, or start without one.

### My session is very slow

* You might be trying to use custom Pixi or Conda environments on a slow 
  filesystem. Try moving them to `/work/` storage.
* Check whether you are running out of RAM: only the memory you selected at
  session creation is guaranteed — see
  [Session resources](scaling-out.md#your-session). Restart the session
  with more RAM if needed.
* Reading many small files from `/depot/` or `/eos/` can be slow — see
  [Data access](data-access.md) for faster access patterns (XCache).

### My session was shut down on its own

Idle sessions are shut down automatically — see
[Idle sessions](scaling-out.md#idle-sessions). Sessions may also occasionally
get shut down due to unplanned outages, so save your work regularly and keep
important code in sync with a Git repository.

### I deleted/broke my configuration and want a clean start

Shut down your session (`File → Hub Control Panel → Stop My Server`), then
start a new one. The session's software is recreated from scratch every time,
but settings kept in your home directory (such as `~/.jupyter` and
`~/.local/share/code-server`) persist: remove or rename the broken ones there.

## Storage

### I can't write to /depot/cms/

Depot is writable only with a Purdue account — see
[Storage volumes](storage.md) for where to write instead.

Purdue users can write to their own private directories, as well as into group directories to which they have access. If you don't have access to your group's directory, please contact facility admins.

### I can't write to /eos/purdue/

Purdue EOS is mounted read-only — see [Writing to EOS](guide-eos-write.md).

### I don't see my Grid directory under /eos/purdue/store/user/

See [Saving outputs of CRAB jobs](storage.md#saving-outputs-of-crab-jobs).

### The `eos-cern` symlink shows up as a file, not a directory

Restart the session (`File → Hub Control Panel → Stop My Server`), then run
the `eos-connect` command again — see [CERNBox access](guide-cern-eos.md).

## Software and kernels

### `pixi shell` / `pixi install` fails in my home directory

This is intentional — see [Pixi storage locations](guide-pixi.md#storage-locations).

### Every cell fails with a Pixi error in the project-aware kernel

The kernel found no Pixi project around the notebook, or cannot use the one
it found; the error names the cause. See what the kernel needs in
[Pixi kernels](software.md#pixi-kernels). For a notebook outside any Pixi
project, choose **Python (pixi global)**.

### My Conda environment doesn't show up as a Jupyter kernel

See [Conda kernels](software.md#conda-kernels).

### A package is missing from the global environment

[Contact support](support.md) — we regularly update the global Pixi environment.
Alternatively, create your own [Pixi environment](guide-pixi.md) with the
packages you need.

## Data access

### XRootD reads fail with authentication errors

Your VOMS proxy is probably missing or expired. Check with `voms-proxy-info`,
and [create a fresh proxy](getting-started.md#6-set-up-a-voms-proxy) if needed.

### A dataset I need is not accessible / only on tape

If no CMS site has the files on disk, a tape recall is necessary: create a
Rucio replication rule to subscribe the dataset to Purdue — see
[Rucio](guide-rucio.md).

## Dask Gateway

### Cluster creation never finishes

`new_cluster()`
[has no time limit](guide-dask-gateway.md#6-cluster-lifetime-and-timeouts).
If it has waited for more than a few minutes, interrupt it,
[stop the pending cluster](guide-dask-gateway.md#5-shutting-down-clusters),
and [contact us](support.md) if it happens again.

### I can't create a cluster: "You may only have 1 active Dask Gateway cluster(s)"

See the [one-cluster limit](guide-dask-gateway.md#limits): shut down your
existing cluster (see
[Shutting down clusters](guide-dask-gateway.md#5-shutting-down-clusters)),
or wait for it to finish stopping, then try again.

### I can't create a cluster: "prometheus_client is not installed"

Add `prometheus_client` to the environment the cluster runs — see
[Pixi or Conda environments](guide-dask-gateway.md#pixi-or-conda-environments).

### Workers fail to start or crash immediately

* Check that the Pixi/Conda environment passed to `new_cluster()` is
  [visible to the workers](storage.md#overview).
* CERN/FNAL users: see the `NB_UID` / `NB_GID` note in
  [Environment variables](guide-dask-gateway.md#environment-variables).

### My cluster disappeared

Idle clusters are shut down automatically — see
[Cluster lifetime and timeouts](guide-dask-gateway.md#6-cluster-lifetime-and-timeouts).

### Workers can't read my data via XRootD

See [Reading data via XRootD](guide-dask-gateway.md#environment-variables).

## Ray

### A submission fails with "Ray clusters can take N more now"

Fewer GPUs are free than you ask for: try again later, or ask for up to
as many with `af-max-workers` — see
[Settings](guide-ray-train.md#settings).

### A submission fails because my cluster "runs other settings, and tasks or jobs"

A job of yours is running with other settings: wait for it to end, or
stop it — see [Settings](guide-ray-train.md#settings).

### "You have no Ray cluster"

A job's log is removed 15 minutes after it ends, and its status an hour after
— see [Good to know](guide-ray-train.md#good-to-know).

### A training stops with "Unable to set up cluster storage"

Set `storage_path` to a directory on `/work` — see
[Data-parallel training](guide-ray-train.md#data-parallel-training).

### A submission fails with "Your session holds a GPU"

Submit from a session started without a GPU — see
[Before you start](guide-ray-train.md#before-you-start).

### A job on NRP stops with "NRP asks each GPU for 40%"

A GPU of your job sat idle: use more data loaders or larger batches, or
run it at Purdue — see [More GPUs on NRP](guide-ray-train.md#more-gpus-on-nrp).

### A job on NRP stops with "did not start in time"

NRP was too busy: ask for fewer workers, or try again later.

### A job on NRP stops with "Copying its inputs to NRP failed"

Check that `af-inputs` is a directory you can read.

### A submission fails with "would be made at the top of"

`af-inputs` and `af-output` go inside a directory of yours, such as
`/work/users/<username>/results`, not directly in `/work` or `/depot/cms`.

### A submission fails with "No token"

Your session has no `RAY_AUTH_TOKEN`: restart it.

## SSH and IDE connections


### Remote-SSH connection from VSCode/Cursor fails

See the [troubleshooting section of the IDE connection guide](guide-ide-connection.md#troubleshooting).
The usual suspects: home directory permissions (`chmod 755 ~/`), a missing or
not-on-PATH `websocat` binary, or an expired JupyterHub token.


## Still stuck?

Send us a message — see [Support](support.md).
