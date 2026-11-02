# The facility

These rules hold for every agent of this analysis, and over the methodology's where they differ.

## Subagents

- Only the orchestrator starts subagents, with the `task` tool. Its `subagent_type` is the name of the role's definition in `agents/` without `.md`, as `note_writer`; an ad-hoc subagent is an `executor`.
- A subagent, which has no `task` tool, does the task the orchestrator gives it, in its first message and in any it is resumed with, and nothing beyond it, then ends with its result. Where the analysis's CLAUDE.md speaks to the orchestrator, a subagent is not it: only the orchestrator moves the analysis on to a next step, phase or gate.
- Where the methodology has a subagent start another or delegate a part of its work, that subagent does the work itself, or ends with what it asks the orchestrator to start.
- The facility sets each role's model: `task` takes none, and the methodology's choices of model do not apply.
- A subagent runs until it ends, never in the background, so there is none to watch. The orchestrator receives only its last message: the files it wrote, its verdict and what is open. A failed one is resumed with its `task_id`, or started afresh.
- Plan mode is writing plan.md before anything else. The facility keeps every session's transcript, so none is exported.

## Run independent work at the same time

- Start subagents whose work does not depend on each other's in one message, as several `task` calls: they run at the same time, as many as the facility has seats for, the others as seats free up, and the results arrive when all have finished.
- What may run at once is in methodology/03a-orchestration.md §3a.5: a review panel's reviewers; the channels of Phases 2-3; shared calibrations beside Phases 2-3; within a phase, independent pieces such as plots, systematic variations, MVA trainings, closure and injection tests. The orchestrator splits them, each to an executor writing its own directory, merged by another before the review.
- Phases stay in order, and a review waits for the work it reviews.

## Commands and where files are

- A command that sets no `timeout` is ended after {minutes} minutes. Give one that runs longer, as a pass over the data, a `timeout` long enough, up to {hours} hours.
- An analysis given input files has them in its `data/`, a link to where they were fetched: `ls data/` lists them, and a search started above `data/` passes over the link unless it is `find -L`. One without `data/` reads its inputs as its prompt says.
- Never search for a file from `/`, `/cvmfs` or `{root}`, whether it is the analysis's, a command or a font: they are too large to search, and such a search runs until its command is ended. Search the analysis's directory and the JFC checkout it is in for its files.
- A command is on the `PATH`, as `command -v NAME` shows, or in the analysis's pixi environment, `.pixi/envs/default`, as `pixi run which NAME` shows; one in neither is not installed, and `pixi add NAME` adds it. Tectonic keeps the TeX files and fonts it fetches in `~/.cache/tectonic`.
- The analysis's pixi environment stays on Python {python}: its tools and the facility's Dask run on it, and another version rebuilds the environment and splits the analysis's results across two interpreters.

## Files, figures and PDFs

- A reply's length is bounded: write a large file in parts. `read` returns a long file in pieces: read on with `offset` to its end.
- `read` shows a PNG to the roles that see images, a limited number per session, each of a limited size; a refused read says why. Review figures from their own PNGs, a few related ones per plot validator, each batch to a fresh one.
- Never `read` a PDF: render its pages with `pdftoppm -r 150 -png`, and use `pdftotext` and `pdfinfo` for its text, unresolved references and page count. Look at pages only for layout, captions and tables.
- A subagent looks only at the figures or pages its prompt names. Where the methodology has one agent look at every figure or page, as it has the reviewers, the typesetter and the rendering reviewer, the orchestrator splits the figures by name and the pages by range into batches, gives each batch to a fresh session of that role, and merges their findings before the arbiter sees them.

## Review a figure by measuring it first

- The orchestrator gives each figure it sends for review its intended content: dataset, luminosity, axis ranges, binning, overflow handling and where a signal is expected.
- Whoever reviews a figure first copies out its axis titles, experiment label and luminosity label as printed, then measures before judging: the lower panel's y-range and the markers or error bars outside it; the first and last x-axis ticks, and whether the last bin is drawn whole; whether the data points of each panel carry error bars; each legend entry against the colour and marker drawn for it. The findings follow from those measurements and the intended content.
- Error bars smaller than their marker are not missing.

## Read CMS data through XCache

Read CMS datasets with uproot from `root://xcache.cms.rcac.purdue.edu//store/...`, the dataset's file names after the prefix. XCache asks for a grid proxy: this container and the analysis's Dask workers hold the facility's, with which DAS answers too (`/cvmfs/cms.cern.ch/common/dasgoclient`). Never copy the proxy anywhere. The environment has xrootd and fsspec-xrootd.

## Process data on Dask Gateway

This container has {cores} cores and there is no batch system: every pass over the events of data or simulation runs on the facility's Dask Gateway, never in a local process, whatever its size. That replaces single-core, multicore, ProcessPoolExecutor and SLURM wherever the methodology names them. Looking at a file's branches or a few of its entries does not need it.

```python
from af_dask import client

dask = client(workers=50)
```

`client()` returns a dask.distributed Client of the analysis's cluster: its workers run this pixi environment, grow with the work up to `workers`, and stop on their own. For a large cluster, make the workers larger rather than more, as in `client(workers=250, cores=4, memory=16)`; the script that starts the cluster sets their size. The gateway's limits are in https://analysis-facility.physics.purdue.edu/guide-dask-gateway/. Split the input by file and entry range, select and fill histograms in the tasks, and bring back histograms, cutflows or small arrays, not events. Many independent fits, such as toys, are tasks too.

- Workers start only once work is submitted, so a check of the cluster submits a task, as `dask.submit(lambda: 1).result(timeout=300)`, instead of waiting for workers.
- A pass runs in the foreground, with a `timeout` long enough for it.
- Workers see {root} as you do, and no other storage but /cvmfs, but start elsewhere: give them absolute paths.
- Workers run as you and write where you do.
- A task can call what its script defines or the environment installs; send a module of the analysis's own with `dask.upload_file(path)`.

## Train on GPUs with Ray

A model that trains on GPUs trains on the facility's Ray, not here:

```python
from af_ray import client

jobs = client(max_workers=4)
job = jobs.submit_job(entrypoint="python /absolute/path/to/train.py")
```

`client()` returns a Ray JobSubmissionClient whose jobs run in this pixi environment, with Ray and PyTorch added to it as https://analysis-facility.physics.purdue.edu/guide-ray-train/ says. Its keyword arguments are the settings of that guide without `af-`: `max_workers=4` is `af-max-workers`. Its scripts show data-parallel training that survives losing GPUs.

- Ray's pods see {root} as you do, and no other storage but /cvmfs: give the entrypoint absolute paths, without a working_dir.
- They run as you, and hold no grid proxy: they read what Dask's tasks wrote, and write checkpoints, models and outputs in the workspace.
