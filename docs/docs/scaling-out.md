# Scaling out

## Session resources

When you start a session, the number of CPU cores (up to 128) and the amount of
memory (up to 128 GB) that you select are **reserved** for your session. A
session can use more — up to 256 cores and 256 GB — while the node it runs on
has spare capacity, but only the reserved amount is guaranteed. If your work
needs more memory than you reserved, restart the session with a larger
selection.

### Idle sessions

Sessions that stay **inactive for 14 days** are shut down automatically.
Sessions holding **any GPU** (A100 slice, full A100, or T4) are shut down after
**24 hours** of inactivity. Your storage volumes are unaffected — simply start a
new session.

## Which method should I use?

When your analysis outgrows a single session, several options are available.
This page gives an overview; detailed instructions are linked from each section.

| Method | Best for | Available to | Scale |
| --- | --- | --- | --- |
| [Dask (local cluster)](guide-dask.md) | Parallelizing Python code within a session | All users | the cores of your session |
| [Dask Gateway, Kubernetes backend](guide-dask-gateway.md) | Distributed Python / Coffea analyses | All users | [hundreds of cores](guide-dask-gateway.md#dask-gateway-at-purdue-af) |
| [Dask Gateway, Slurm backend](guide-dask-gateway.md) | Distributed Python / Coffea analyses | Purdue users | Hammer cluster |
| Slurm batch jobs | Independent batch workloads, GPU jobs | Purdue users | Hammer cluster (`cms` account) or other Purdue Community Clusters |
| CRAB | CMSSW (`cmsRun`) jobs, MC generation, skimming | All CMS users | the entire WLCG |

## Dask

[Dask](https://docs.dask.org/en/stable/) is an open-source library for parallel
computing in Python. It can be used to
[quickly parallelize any Python code](guide-dask.md), or implicitly as a backend in
frameworks such as Coffea and RDataFrame.

* A **local Dask cluster** parallelizes your code over the cores of your own
  session — no extra setup required.
* **[Dask Gateway](guide-dask-gateway.md)** scales beyond the session, submitting
  workers either as Kubernetes pods on the Geddes cluster (all users), or as Slurm
  jobs on the Hammer cluster (Purdue users only).

## Slurm (Purdue users only)

[Slurm](https://slurm.schedmd.com/documentation.html) is a job scheduler and
workload manager that enables batch submission on Purdue computing clusters.
At Purdue AF, **users with local Purdue accounts** can submit jobs from the AF
terminal to the Hammer cluster, using the `cms` Slurm account. Users can also submit Slurm jobs at other Community Clusters after logging into them via `ssh`.

* [Instructions for submitting Slurm jobs](https://www.rcac.purdue.edu/knowledge/hammer/run)
* Code and data used by Slurm jobs must be stored on a volume that Slurm jobs
  can see — see [Storage volumes](storage.md).
* To request a GPU for a Slurm job, see [GPU access](gpus.md#2-slurm-jobs-purdue-users-only).

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

Slurm and Dask metrics are available in the corresponding sections of the
[Purdue AF monitoring dashboard](https://cms.geddes.rcac.purdue.edu/grafana/d/purdue-af-dashboard/purdue-analysis-facility-dashboard){ target="_blank" }.
Each Dask Gateway cluster additionally gets its own Dask dashboard — see
[Dask Gateway monitoring](guide-dask-gateway.md#3-monitoring).
