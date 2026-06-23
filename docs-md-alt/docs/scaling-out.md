# Scaling out

A single Purdue AF session is limited to **128 CPU cores and 128 GB RAM**.
When your analysis outgrows these resources, several options are available.
This page gives an overview; detailed instructions are linked from each section.

## Which method should I use?

| Method | Best for | Available to | Typical scale |
| --- | --- | --- | --- |
| [Dask (local cluster)](guide-dask.md) | Parallelizing Python code within a session | All users | up to 128 cores |
| [Dask Gateway, Kubernetes backend](guide-dask-gateway.md) | Distributed Python / Coffea analyses | All users | up to 200 workers (200 cores, 1.2 TB RAM) |
| [Dask Gateway, Slurm backend](guide-dask-gateway.md) | Distributed Python / Coffea analyses | Purdue users | hundreds of workers (Hammer / Gautschi) |
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
  jobs on the Hammer or Gautschi clusters (Purdue users only). Note that each user
  can have **at most one active Dask Gateway cluster** per gateway at a time.

## Slurm (Purdue users only)

[Slurm](https://slurm.schedmd.com/documentation.html) is a job scheduler and
workload manager that enables batch submission on Purdue computing clusters.
At Purdue AF, **users with local Purdue accounts** can submit jobs from the AF
terminal to the Hammer cluster, using the `cms` Slurm account. Users can also submit Slurm jobs at other Community Clusters after logging into them via `ssh`.

* [Instructions for submitting Slurm jobs](https://www.rcac.purdue.edu/knowledge/hammer/run)
* Code and data used by Slurm jobs must be stored on **Depot** (`/depot/cms/`) —
  `/home/` and `/work/` are not mounted in Slurm jobs
  (see [Storage volumes](storage.md)).
* To request a GPU for a Slurm job, add `--gpus-per-node=1` to the `sbatch`
  command — see [GPU access](gpus.md).

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
* CRAB outputs are written to your Grid directory at Purdue EOS:
  `/eos/purdue/store/user/<cern-username>` — see [Storage volumes](storage.md).

## Monitoring your jobs

Slurm and Dask metrics are available in the corresponding sections of the
[Purdue AF monitoring dashboard](https://cms.geddes.rcac.purdue.edu/grafana/d/purdue-af-dashboard/purdue-analysis-facility-dashboard){ target="_blank" }.
Each Dask Gateway cluster additionally gets its own Dask dashboard — see
[Dask Gateway monitoring](guide-dask-gateway.md#3-monitoring).
