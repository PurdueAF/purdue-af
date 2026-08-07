## Purdue Analysis Facility — platform context

You are in a Purdue AF session: a JupyterHub pod on the Purdue Geddes Kubernetes
cluster. **Rules** below are enforced by the platform and fail if broken;
**guidance** is what usually works best — deviate when the user has a reason.

The `purdue-af-agentic-interface` MCP server is registered and authenticated with
this session's credentials. Use it for session, Dask, storage and log queries; it
is self-describing, so call a tool and follow its result.

### Session

| Option | Values |
| --- | --- |
| CPUs | 4, 16, 32, 64, 128 |
| Memory | 16, 32, 64, 128 GB |
| GPU | none, 1 A100 slice (5 GB), 1 full A100 (40 GB), 1 NVIDIA T4 (16 GB) |
| Interface | JupyterLab, VS Code (code-server) |

One session is capped at 128 cores / 128 GB RAM. Sessions holding any GPU are
culled after 24 h idle; all others after 14 days.

### Storage

| Path | Size per user | Access | Visible to Slurm jobs and Dask/Slurm workers | Visible to Dask/k8s workers |
| --- | --- | --- | --- | --- |
| `/home/<username>/` | 25 GB (hard quota) | read/write | no | no |
| `/work/users/<username>/` | 100 GB | read/write | no | yes |
| `/work/projects/<project>/` | up to 1 TB | read/write | no | yes |
| `/depot/cms/` | up to 1 TB | read/write for Purdue accounts, read-only for others | yes | yes |
| `/eos/purdue/` | up to 100 TB | read-only over POSIX; write via `gfal`/`xrdcp` | yes | yes |
| `/cvmfs/` | n/a | read-only | yes | yes |
| `/eos/cern/` (CERNBox) | n/a | read/write, mounted on request | no | no |

Home-directory shortcuts, all symlinks to the paths above: `~/work` → `/work/`,
`~/eos-purdue` → `/eos/purdue`, `~/depot/users` → `/depot/cms/users`, plus
`~/depot/{hh,hmm,top,sonic}` → `/depot/cms/<name>`. `/depot/cms/users` is itself
a symlink to `/depot/cms/private/users`, so `~/depot/users/<u>`,
`/depot/cms/users/<u>` and `/depot/cms/private/users/<u>` are one directory.
Other top-level `/work` entries: `projects`, `users`, `pixi`, `triton_models`.
`/eos` holds `purdue` and `cern`.

**Rules.** Exceeding the 25 GB home quota prevents the session from starting at
all. `/eos/purdue/` cannot be written through the POSIX mount. `/home` and
`/work` do not exist inside Slurm jobs or Dask/Slurm workers.

**Guidance.** Keep code and environments off `/home` — it is small and invisible
to every worker. Writing many files to `/depot` at once degrades it for everyone;
staging to `/tmp` on the worker and copying once is gentler.

### Software environments

Pixi is the platform's package manager, and `pixi` here is a wrapper, not
upstream pixi.

**Rules.** Project commands (`add`, `install`, `shell`, `update`, `init`,
`remove`, …) **refuse to run on a project under `/home/`**; the wrapper exits
with an error naming the directory. This is deliberate — pixi environments are
large and would exhaust the 25 GB home quota. `PIXI_HOME` and `PIXI_CACHE_DIR`
are preset under `/work/users/<username>/` for the same reason, and pointing
them back under `/home` is refused too. A pixi or conda environment becomes a Jupyter kernel only if it has
`ipykernel` installed and sits in a world-readable directory, which
`/depot/cms/private/` directories are not. Environments used from Slurm jobs or
Dask/Slurm workers must live on `/depot`, the only writable volume those workers
see.

The shared environment is `/work/pixi/global/` (manifest plus
`.pixi/envs/default`). It is generated from the platform repository and
re-synchronised automatically, so edits made in place are overwritten.

**On PATH in a terminal:** `pixi`, `conda`, `rucio`, `kinit`, `gfal-*`,
`voms-proxy-init`, `xrdcp`/`xrdfs`, `sbatch`, `squeue`. ROOT is deliberately not
in the base environment — it belongs to an analysis environment, so expect it
from the global env or a project env, never from the bare session.

CERNBox is mounted with `eos-connect`, a shell alias for
`source /etc/jupyter/eos-connect.sh`. Aliases only exist in interactive shells,
so use the `source` form from a script.

Kernels shipped with the image: `python3`, and "Python (pixi project-aware)"
which discovers the environment beside the notebook and falls back to the global
one. Conda, LCG and user-created kernels appear as they are installed.

CVMFS repositories mounted: `cms.cern.ch`, `cms-af.opensciencegrid.org`,
`cms-bril.cern.ch`, `cms-griddata.cern.ch`, `config-osg.opensciencegrid.org`,
`oasis.opensciencegrid.org`, `sft.cern.ch`.

The session image is Python 3.12 with CUDA 12.4 and cuDNN 8.9.7.29; ML packages
must match those versions.

### Data access

**Rules.** Remote CMS data is read over XRootD and requires a valid VOMS proxy
(`voms-proxy-init`). XCache does not serve tape-only files; those need a Rucio
replication rule. Rucio-subscribed datasets land under `/eos/purdue/store/mc/` or
`/eos/purdue/store/data/`, never in user directories. CRAB outputs land in
`/eos/purdue/store/user/<cern-username>` — the CERN username, which differs from
the Purdue one.

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

**Rules.** At most one active Dask Gateway cluster per user per gateway; creating
another requires stopping the existing one. `Gateway()` with no arguments
connects to the **Kubernetes** backend (`DASK_GATEWAY__ADDRESS` is preset). A
Slurm backend requires the address explicitly:

    Gateway("http://dask-gateway-k8s-slurm.geddes.rcac.purdue.edu/",
            proxy_address="api-dask-gateway-k8s-slurm.cms.geddes.rcac.purdue.edu:8000")

Hammer is the only Slurm backend; there is no Gautschi Dask gateway.

### GPUs

**Rules.** Slurm GPU jobs need `--gpus-per-node=1`. `/depot` is the only volume
shared between the AF and the Gilbreth cluster (`ssh gilbreth`).

Facility-wide there are 14 A100 5 GB slices, 4 full 40 GB A100s, and T4s
(`nvidia.com/gpu`); the spawn form shows live availability. Any GPU session is
culled after 24 h idle. Hammer has 22 nodes with NVIDIA T4 for Slurm. A session
without a GPU has no `nvidia-smi`.

Full user documentation: https://analysis-facility.physics.purdue.edu
