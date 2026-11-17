# Computing resources

All your work starts in your session. When it outgrows the session, you send it
elsewhere from the same session — a notebook or a terminal there is all you
need.

## Where to run your work

| You want to | Use | Available to |
| --- | --- | --- |
| Work interactively on up to 128 cores | [Your session](#your-session) | All users |
| Spread Python code over the cores of your session | [A local Dask cluster](guide-dask.md) | All users |
| Run a Coffea or Python analysis on [hundreds of cores](guide-dask-gateway.md#limits) | [Dask Gateway](guide-dask-gateway.md) | All users |
| Work interactively on one GPU | [A session with a GPU](gpus.md) | All users |
| Train or run inference on [several GPUs](guide-ray-train.md#good-to-know), at Purdue or on NRP | [Multi-GPU training](guide-ray-train.md) | All users, from a session without a GPU |
| Run independent batch jobs, with or without GPUs | [Slurm](#slurm-purdue-users-only) | Purdue accounts |
| Run CMSSW (`cmsRun`) jobs, such as MC generation or skimming | [CRAB](#crab) | All CMS users |

## Your session

The CPU cores (up to 128) and the memory (up to 128 GB) you select when you
start a session are **reserved** for it. The session can use more — up to 256
cores and 256 GB — while its node has spare capacity, but only the reserved
amount is guaranteed. If your work needs more memory than you reserved, restart
the session with a larger selection.

A session can also hold a GPU — see [GPUs in a session](gpus.md).

### Idle sessions

A session that stays **inactive for 14 days** is shut down; one holding **any
GPU** (A100 slice, full A100, or T4) is shut down after **24 hours** of
inactivity. Your storage volumes are unaffected — simply start a new session.

## Slurm (Purdue users only)

With a Purdue account, you submit [Slurm](https://slurm.schedmd.com/documentation.html)
jobs from an AF terminal to the Hammer cluster, under the `cms` account — see
[RCAC's instructions](https://docs.rcac.purdue.edu/userguides/hammer/run_jobs/).
You can also log in to other Purdue Community Clusters with `ssh` and submit
there.

* **Storage**: a Slurm job sees only `/depot` and `/cvmfs`, so keep its code,
  data and outputs there — see [Storage volumes](storage.md).
* **GPUs**: add `--gpus-per-node=1` to `sbatch`. Hammer has **Nvidia T4**
  GPUs. For more GPUs or other models, use the
  [Gilbreth cluster](https://www.rcac.purdue.edu/compute/gilbreth): run
  `ssh gilbreth` in an AF terminal, log in with BoilerKey, and submit there.
  Gilbreth shares only `/depot/` with Purdue AF, so write job outputs there.

## CRAB

[CRAB](https://twiki.cern.ch/twiki/bin/view/CMSPublic/SWGuideCrab) submits
CMSSW jobs to CMS sites worldwide over the WLCG, and reads any CMS dataset where
it is stored. Use it for compute-heavy CMSSW work, such as
[Monte Carlo generation](guide-mc-gen.md) or skimming AOD and MiniAOD datasets.

* [Instructions for submitting CRAB jobs](https://www.physics.purdue.edu/Tier2/user-info/tutorials/crab3.php)
* Outputs land in your Grid directory on Purdue EOS — see
  [Saving outputs of CRAB jobs](storage.md#saving-outputs-of-crab-jobs).

## Monitoring your jobs

The [Purdue AF monitoring dashboard](https://cms.geddes.rcac.purdue.edu/grafana/d/purdue-af-dashboard/purdue-analysis-facility-dashboard){ target="_blank" }
has sections for Slurm, Dask and Ray. Each Dask Gateway cluster also has a Dask
dashboard of its own — see
[Dask Gateway monitoring](guide-dask-gateway.md#3-monitoring).
