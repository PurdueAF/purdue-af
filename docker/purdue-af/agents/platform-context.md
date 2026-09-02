## Purdue Analysis Facility — platform context

You are in a Purdue AF session: a JupyterHub pod on the Purdue Geddes Kubernetes
cluster. **Rules** below are enforced by the platform and fail if broken;
**guidance** is what usually works best — deviate when the user has a reason.

The `purdue-af-agentic-interface` MCP server is registered and authenticated with
this session's credentials. It is self-describing: call a tool and follow its
result. Use it, not the shell, for storage and quotas (never `du`/`df`), logs
(never tailing files), and session or cluster state (never `kubectl`/`ps`) —
quotas are enforced outside the filesystem, and a session cannot see its own pod.

### Session

CPUs 4, 16, 32, 64 or 128; memory 16, 32, 64 or 128 GB; GPU none, 1 A100 slice
(5 GB), 1 full A100 (40 GB) or 1 NVIDIA T4 (16 GB); interface JupyterLab or
VS Code. `list_af_profiles` has the exact option keys and live GPU availability.

**Rules.** One session is capped at 128 cores / 128 GB RAM. A GPU session is
culled after 24 h idle, any other after 14 days. Without a GPU there is no
`nvidia-smi`.

### Storage

| Path | Per user | Access | Slurm and Dask/Slurm workers | Dask/k8s workers |
| --- | --- | --- | --- | --- |
| `/home/<username>/` | 25 GB (hard quota) | read/write | no | no |
| `/work/users/<username>/` | 100 GB | read/write | no | yes |
| `/work/projects/<project>/` | up to 1 TB | read/write | no | yes |
| `/depot/cms/` | up to 1 TB | read/write for Purdue accounts, read-only for others | yes | yes |
| `/eos/purdue/` | up to 100 TB | read-only over POSIX; write via `gfal`/`xrdcp` | yes | yes |
| `/cvmfs/` | n/a | read-only | yes | yes |
| `/eos/cern/` (CERNBox) | n/a | read/write, mounted on request | no | no |

Symlinks in home: `~/work` → `/work/`, `~/eos-purdue` → `/eos/purdue`,
`~/depot/users` → `/depot/cms/users` (itself a symlink to
`/depot/cms/private/users`), `~/depot/{hh,hmm,top,sonic}` → `/depot/cms/<name>`.

**Rules.** Exceeding the 25 GB home quota prevents the session from starting at
all. `/eos/purdue/` cannot be written through the POSIX mount. `/home` and
`/work` do not exist inside Slurm jobs or Dask/Slurm workers.

**Guidance.** Keep code and environments off `/home` — small, and invisible to
every worker. Writing many files to `/depot` at once degrades it for everyone;
stage to the worker's `/tmp` and copy once.

### Software environments

`pixi` here is a wrapper, not upstream pixi.

**Rules.** Project commands (`add`, `install`, `shell`, `update`, `init`,
`remove`, …) **refuse to run on a project under `/home/`**, and `PIXI_HOME` /
`PIXI_CACHE_DIR` are preset under `/work/users/<username>/` — pointing them back
under `/home` is refused too. An environment becomes a Jupyter kernel only with
`ipykernel` installed and a world-readable directory, which `/depot/cms/private/`
is not. Environments used from Slurm or Dask/Slurm workers must live on `/depot`,
the only writable volume those workers see. The shared environment
`/work/pixi/global/` is regenerated from the platform repository, so edits in
place are overwritten.

**On PATH in a terminal:** `pixi`, `conda`, `rucio`, `kinit`, `gfal-*`,
`voms-proxy-init`, `xrdcp`/`xrdfs`, `sbatch`, `squeue`. ROOT is deliberately not
in the base environment — expect it from the global or a project env, never from
the bare session.

The image is Python 3.12 with CUDA 12.4 and cuDNN 8.9.7.29; ML packages must
match. CERNBox mounts with `eos-connect`, an alias — use
`source /etc/jupyter/eos-connect.sh` from a script.

### Data access

**Rules.** Remote CMS data is read over XRootD and requires a valid VOMS proxy
(`voms-proxy-init`). XCache does not serve tape-only files; those need a Rucio
replication rule. Rucio-subscribed datasets land under `/eos/purdue/store/`,
never in user directories. CRAB outputs land in
`/eos/purdue/store/user/<cern-username>` — the CERN username, not the Purdue one.

**Guidance.** For repeated reads of one remote dataset, prefer the XCache prefix
`root://xcache.cms.rcac.purdue.edu/`; it also removes the need to know which site
holds the file. A dataset need not be at Purdue to be read.

### Scale-out

| Method | Available to | Limits |
| --- | --- | --- |
| Local Dask cluster | all users | the session's own cores (≤ 128) |
| Dask Gateway, Kubernetes | all users | ≤ 200 workers; ≤ 64 cores and ≤ 64 GiB per worker |
| Dask Gateway, Slurm (Hammer) | Purdue accounts | partition `hammer-nodes`, account `cms`, 4 h walltime; ≤ 16 cores, ≤ 64 GiB per worker |
| Slurm batch (`sbatch`) | Purdue accounts | Hammer, account `cms`; `/depot` only |
| CRAB | all CMS users | WLCG |

**Rules.** At most one active Dask Gateway cluster per user per gateway. Hammer
is the only Slurm backend. Slurm GPU jobs need `--gpus-per-node=1`. `/depot` is
the only volume shared with the Gilbreth cluster (`ssh gilbreth`). `Gateway()`
with no arguments connects to **Kubernetes** (`DASK_GATEWAY__ADDRESS` is preset);
Slurm needs the address explicitly:

    Gateway("http://dask-gateway-k8s-slurm.geddes.rcac.purdue.edu/",
            proxy_address="api-dask-gateway-k8s-slurm.cms.geddes.rcac.purdue.edu:8000")

Full user documentation: https://analysis-facility.physics.purdue.edu
