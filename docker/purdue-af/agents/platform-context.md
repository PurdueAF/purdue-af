## Purdue Analysis Facility — platform context

You are in a Purdue AF session: a JupyterHub pod on the Purdue Geddes Kubernetes
cluster, running as the user with their storage mounted. **Rules** below are
enforced by the platform and fail if broken; **guidance** is what usually works
best — deviate when the user has a reason.

The `purdue-af-agentic-interface` MCP server is registered and authenticated as
this user, so it needs no token handling. It is self-describing: call a tool and
follow its result rather than planning a whole sequence up front. Prefer it to
the shell wherever the platform tracks something centrally — the shell will
answer confidently and wrongly:

- **Storage and quotas** — `query_storage_usage`, never `du`/`df`. Quotas are
  per-user and enforced outside the filesystem, so `du` reports neither.
- **Logs** — `query_notebook_logs` / `query_dask_logs`, never tailing files.
  They come from Loki and outlive the pod that produced them.
- **Session and cluster state** — the tools, never `kubectl` or `ps`. A session
  has no permission to see its own pod object.

### Session

| Option | Values |
| --- | --- |
| CPUs | 4, 16, 32, 64, 128 |
| Memory | 16, 32, 64, 128 GB |
| GPU | none, 1 A100 slice (5 GB), 1 full A100 (40 GB), 1 NVIDIA T4 (16 GB) |
| Interface | JupyterLab, VS Code (code-server) |

The list above is what exists; `list_af_profiles` carries the exact option keys
to pass when starting a session, plus which GPUs are free right now.

**Rules.** One session is capped at 128 cores / 128 GB RAM. Sessions holding any
GPU are culled after 24 h idle, all others after 14 days. A session without a GPU
has no `nvidia-smi` and no visible CUDA device, so code must fall back to CPU
rather than assume one appeared. Restarting or stopping the session kills every
process in it — including an agent running inside it — while `/home`, `/work`
and `/depot` survive untouched.

### Storage

Which volumes a worker can see is what most often breaks an otherwise correct
job — a path that works in the notebook may not exist on the worker.

| Path | Size per user | Access | Visible to Slurm jobs and Dask/Slurm workers | Visible to Dask/k8s workers |
| --- | --- | --- | --- | --- |
| `/home/<username>/` | 25 GB (hard quota) | read/write | no | no |
| `/work/users/<username>/` | 100 GB | read/write | no | yes |
| `/work/projects/<project>/` | up to 1 TB | read/write | no | yes |
| `/depot/cms/` | up to 1 TB | read/write for Purdue accounts, read-only for others | yes | yes |
| `/eos/purdue/` | up to 100 TB | read-only over POSIX; write via `gfal`/`xrdcp` | yes | yes |
| `/cvmfs/` | n/a | read-only | yes | yes |
| `/eos/cern/` (CERNBox) | n/a | read/write, mounted on request | no | no |

The session's own username is `whoami` (also `$NB_USER`) and `$HOME` is
`/home/<username>`; build paths from those rather than guessing the name.

Home-directory shortcuts, all symlinks to the paths above: `~/work` → `/work/`,
`~/eos-purdue` → `/eos/purdue`, `~/depot/users` → `/depot/cms/users`, plus
`~/depot/{hh,hmm,top,sonic}` → `/depot/cms/<name>`. `/depot/cms/users` is itself
a symlink to `/depot/cms/private/users`, so `~/depot/users/<u>`,
`/depot/cms/users/<u>` and `/depot/cms/private/users/<u>` are one directory, not
three copies. Other top-level `/work` entries: `projects`, `users`, `pixi`,
`triton_models`. `/eos` holds `purdue` and `cern`.

**Rules.** Exceeding the 25 GB home quota prevents the session from starting at
all — the next spawn fails, not merely the write. `/eos/purdue/` cannot be
written through the POSIX mount; write there with `gfal-copy` or `xrdcp`.
`/home` and `/work` do not exist inside Slurm jobs or Dask/Slurm workers.

**Guidance.** Keep code, environments and outputs off `/home` — it is small and
invisible to every worker. `/work/users/<username>/` is the usual home for a
project; use `/depot` when Slurm or Dask/Slurm workers have to read it. Writing
many files to `/depot` at once degrades it for everyone, so stage to `/tmp` on
the worker and copy once.

### Software environments

Pixi is the platform's package manager, and `pixi` here is a wrapper, not
upstream pixi — it enforces the rules below rather than reporting them.

**Rules.** Project commands (`add`, `install`, `shell`, `update`, `init`,
`remove`, …) **refuse to run on a project under `/home/`**; the wrapper exits
with an error naming the directory. This is deliberate — pixi environments are
large and would exhaust the 25 GB home quota. `PIXI_HOME` and `PIXI_CACHE_DIR`
are preset under `/work/users/<username>/` for the same reason, and pointing
them back under `/home` is refused too. A pixi or conda environment becomes a
Jupyter kernel only if it has `ipykernel` installed and sits in a world-readable
directory, which `/depot/cms/private/` directories are not. Environments used
from Slurm jobs or Dask/Slurm workers must live on `/depot`, the only writable
volume those workers see.

**Guidance.** Create the project under `/work/users/<username>/` and run `pixi
init` / `pixi add` there; put it on `/depot` instead when Slurm or Dask/Slurm
workers must import it. The shared environment `/work/pixi/global/` is
generated from the platform repository and re-synchronised automatically, so
edits made in place are overwritten — copy it into a project rather than
modifying it.

**On PATH in a terminal:** `pixi`, `conda`, `rucio`, `kinit`, `gfal-*`,
`voms-proxy-init`, `xrdcp`/`xrdfs`, `sbatch`, `squeue`. ROOT is deliberately not
in the base environment — it belongs to an analysis environment, so expect it
from the global env or a project env, never from the bare session.

Besides `python3`, the image ships a "Python (pixi project-aware)" kernel that
discovers the environment beside the notebook and falls back to the global one;
conda, LCG and user-created kernels appear as they are installed. CVMFS
repositories mounted: `cms.cern.ch`, `cms-af.opensciencegrid.org`,
`cms-bril.cern.ch`, `cms-griddata.cern.ch`, `config-osg.opensciencegrid.org`,
`oasis.opensciencegrid.org`, `sft.cern.ch`.

CERNBox is mounted with `eos-connect`, a shell alias for
`source /etc/jupyter/eos-connect.sh`; aliases exist only in interactive shells,
so use the `source` form from a script. The session image is Python 3.12 with
CUDA 12.4 and cuDNN 8.9.7.29; ML packages must match those versions.

### Data access

**Rules.** Remote CMS data is read over XRootD and requires a valid VOMS proxy
(`voms-proxy-init`); without one, reads fail with an authentication error rather
than a missing file. A proxy expires, so a long job can start fine and fail
partway — check with `voms-proxy-info` and renew rather than assuming
yesterday's is still valid. XCache does not serve tape-only files; those need a
Rucio replication rule first. Rucio-subscribed datasets land under
`/eos/purdue/store/mc/` or `/eos/purdue/store/data/`, never in user directories.
CRAB outputs land in `/eos/purdue/store/user/<cern-username>` — the CERN
username, which differs from the Purdue one, so a path built from the session's
own username will not exist.

**Guidance.** For repeated reads of one remote dataset, prefer the XCache prefix
`root://xcache.cms.rcac.purdue.edu/`; it also removes the need to know which site
holds the file. A dataset does not need to be at Purdue to be read.

### Scale-out

| Method | Available to | Limits |
| --- | --- | --- |
| Local Dask cluster | all users | the session's own cores (≤ 128) |
| Dask Gateway, Kubernetes | all users | ≤ 200 workers; ≤ 64 cores and ≤ 64 GiB per worker |
| Dask Gateway, Slurm (Hammer) | Purdue accounts | partition `hammer-nodes`, account `cms`, 4 h walltime; ≤ 16 cores, ≤ 64 GiB per worker |
| Slurm batch (`sbatch`) | Purdue accounts | Hammer, account `cms`; `/depot` only |
| CRAB | all CMS users | WLCG |

**Rules.** A Dask Gateway cluster runs the environment you name, and does not
inherit the notebook's: pass either `conda_env` (a path) or `pixi_project` (a
project directory, with `pixi_env` defaulting to `default`) — one is required
and the two are mutually exclusive. The gateway checks that the environment
exists and is already built before the cluster starts, so run `pixi install`
first. That environment has to sit where the workers can read it — `/work` or
`/depot` for Kubernetes, `/depot` only for Slurm — and to contain every package
the analysis imports.

At most one active Dask Gateway cluster per user per gateway; creating another
requires stopping the existing one. `Gateway()` with no arguments
connects to the **Kubernetes** backend (`DASK_GATEWAY__ADDRESS` is preset), so a
Slurm cluster is never what you get by default — it requires the address
explicitly:

    Gateway("http://dask-gateway-k8s-slurm.geddes.rcac.purdue.edu/",
            proxy_address="api-dask-gateway-k8s-slurm.cms.geddes.rcac.purdue.edu:8000")

Hammer is the only Slurm backend; there is no Gautschi Dask gateway. Slurm GPU
jobs need `--gpus-per-node=1`. `/depot` is the only volume shared between the AF
and the Gilbreth cluster (`ssh gilbreth`).

**Guidance.** Match the backend to the data: Dask/k8s workers see `/work` and
`/depot`, Dask/Slurm workers see only `/depot`, so a job reading `/work` has to
run on Kubernetes — start there unless the work needs Slurm-only resources. A
local cluster is bounded by the session's own cores, so sizing one past the CPUs
the session was started with only adds contention. `list_dask_cluster_options`
carries the option keys and per-gateway limits.

Full user documentation: https://analysis-facility.physics.purdue.edu
