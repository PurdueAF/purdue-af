# Computing resources

You work in your session. When an analysis outgrows it, the same session sends
work to Dask Gateway, Ray, Slurm or CRAB.

## Your session

When you start a session, the number of CPU cores (up to 128) and the amount of
memory (up to 128 GB) that you select are **reserved** for your session. A
session can use more — up to 256 cores and 256 GB — while the node it runs on
has spare capacity, but only the reserved amount is guaranteed. If your work
needs more memory than you reserved, restart the session with a larger
selection. A session can also hold a GPU — see [GPUs in a session](gpus.md).

### Idle sessions

Sessions that stay **inactive for 14 days** are shut down automatically.
Sessions holding **any GPU** (A100 slice, full A100, or T4) are shut down after
**24 hours** of inactivity. Your storage volumes are unaffected — simply start a
new session.

## Which method should I use?

| Method | Best for | Available to | Scale |
| --- | --- | --- | --- |
| Your session | Interactive work, one GPU | All users | the cores, memory and [GPU](gpus.md) you selected |
| [Dask (local cluster)](guide-dask.md) | Parallelizing Python code within a session | All users | the cores of your session |
| [Dask Gateway](guide-dask-gateway.md) | Distributed Python / Coffea analyses | All users | [hundreds of cores](guide-dask-gateway.md#limits) |
| [Ray](guide-ray-train.md) | Training and inference on several GPUs | All users, from sessions without a GPU | [shared GPUs, at Purdue and on NRP](guide-ray-train.md#good-to-know) |
| [Slurm](#slurm-purdue-users-only) | Independent batch jobs, with or without GPUs | Purdue users | Hammer cluster (`cms` account) or other Purdue Community Clusters |
| [CRAB](#crab) | CMSSW (`cmsRun`) jobs, MC generation, skimming | All CMS users | the entire WLCG |

## Slurm (Purdue users only)

[Slurm](https://slurm.schedmd.com/documentation.html) is a job scheduler and
workload manager that enables batch submission on Purdue computing clusters.
At Purdue AF, **users with local Purdue accounts** can submit jobs from the AF
terminal to the Hammer cluster, using the `cms` Slurm account. Users can also
submit Slurm jobs at other Community Clusters after logging into them via `ssh`.

* [Instructions for submitting Slurm jobs](https://docs.rcac.purdue.edu/userguides/hammer/run_jobs/)
* Code and data used by Slurm jobs must be stored on a volume that Slurm jobs
  can see — see [Storage volumes](storage.md).
* **GPUs**: add `--gpus-per-node=1` to the `sbatch` command. Jobs submitted
  from the AF run on Hammer, which has **Nvidia T4** GPUs. For more GPUs or
  other models, submit on the
  [Gilbreth cluster](https://www.rcac.purdue.edu/compute/gilbreth): run
  `ssh gilbreth` in an AF terminal and log in with BoilerKey. The only volume
  shared between Purdue AF and Gilbreth is `/depot/`, so write job outputs
  there.

## CRAB

[CRAB](https://twiki.cern.ch/twiki/bin/view/CMSPublic/SWGuideCrab)
(CMS Remote Analysis Builder) is a utility to submit CMSSW jobs to distributed
computing resources. CRAB allows you to:

* access Data and Monte Carlo datasets stored at any CMS computing site worldwide;
* exploit the CPU and storage resources of CMS computing sites via the Worldwide
  LHC Computing Grid (WLCG).

CRAB is suitable for running most CMSSW framework jobs (i.e. jobs launched via the
`cmsRun` command). It is recommended for computationally intensive workloads such
as [Monte Carlo generation](guide-mc-gen.md) or "skimming" AOD / MiniAOD datasets.

* [Instructions for submitting CRAB jobs](https://www.physics.purdue.edu/Tier2/user-info/tutorials/crab3.php)
* CRAB outputs are written to your Grid directory at Purdue EOS — see
  [Saving outputs of CRAB jobs](storage.md#saving-outputs-of-crab-jobs).

## Monitoring your jobs

Slurm, Dask and Ray metrics are available in the corresponding sections of the
[Purdue AF monitoring dashboard](https://cms.geddes.rcac.purdue.edu/grafana/d/purdue-af-dashboard/purdue-analysis-facility-dashboard){ target="_blank" }.
Each Dask Gateway cluster additionally gets its own Dask dashboard — see
[Dask Gateway monitoring](guide-dask-gateway.md#3-monitoring).
