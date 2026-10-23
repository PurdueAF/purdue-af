# agentic-analysis

Runs physics analyses unattended on [Flyte 2](https://www.union.ai/docs/v2/flyte/)
with the [JFC](https://github.com/jfc-mit/jfc) methodology: Claude Code,
following the JFC specification, takes an analysis from its prompt to a typeset
analysis note. The control plane is [`apps/flyte`](../../apps/flyte); the task
pods, their directory on `/work`, the volume and the model credentials are
[`apps/agentic-analysis`](../../apps/agentic-analysis); the task image is
[`docker/agentic-analysis`](../../docker/agentic-analysis). An analysis is a
directory under [`analyses/`](analyses) with an `analysis.toml`, such as
[`h4l`](analyses/h4l) or [`hmm`](analyses/hmm).

| File                  | What it is                                                   |
| --------------------- | ------------------------------------------------------------ |
| `agentic_analysis.py` | The task, its environment and the launcher (`__main__`)      |
| `af_profiling.py`     | The profiler hook installed into each analysis's environment |
| `af_dask.py`          | The Dask cluster helper installed into the same environment  |
| `af_ray.py`           | The Ray jobs helper installed into the same environment      |
| `browse.sh`           | Opens the `agentic-analysis` volume read-only                |
| `config.yaml`         | Where `flyte` finds the control plane                        |
| `analyses/`           | The analyses                                                 |

A run is one `advance` task: the setup if the workspace is new, then one
headless Claude Code turn. It ends when the agent ends its turn, either with
the analysis note finished or with a message for a person. JFC stops that way
to show the draft note before it looks at the full data. A turn that the Claude
account's usage limit stops resumes within the run once the limit lifts: at
the reset Claude Code reports, or else when a request outside the session goes
through, asked every `LIMITED` minutes. The task pod keeps its resources
meanwhile.

Before each turn the runner writes `FACILITY` as Claude Code's user memory,
which the agent and every subagent read: how this facility runs subagents,
reads CMS data through XCache with the grid proxy the task pods hold, processes
it on [Dask Gateway](../../apps/dask-gateway) with `af_dask.client()`, and
trains on GPUs through [Ray](../../apps/ray-train) with `af_ray.client()`.
Whose those clusters are:
[`apps/agentic-analysis`](../../apps/agentic-analysis/README.md#secrets). In the
workspace, they write only to `shared/`.

A workspace is `/work/projects/agentic-analysis/<name of its first run>`: the JFC checkout
with the analysis scaffolded in it, its pixi environment and git history, the
agent's session, the replies it was given, the tokens its runs have processed,
the last run's output in `said.md`, and `shared/` for what the clusters write.
An analysis's data is fetched once per URL under `/work/projects/agentic-analysis/data`. A
retried task resumes the session.

Every Python process in an analysis's environment, its Dask cluster's
included, sends CPU profiles to [Pyroscope](../../apps/monitoring/pyroscope),
under `service_name="agentic-analysis"` and tagged with its `analysis`, `workspace` and
`script`. The setup keeps that environment's Python within the versions
`pyroscope-io` ships wheels for, adds `CONDA` and `PACKAGES`, and installs the
three helpers, with a `.pth` file that starts `af_profiling`.

## Running

From an AF session, in a checkout of this repository, with the directory of an
analysis:

```bash
uv run --project docker/agentic-analysis --frozen python workflows/agentic-analysis/agentic_analysis.py workflows/agentic-analysis/analyses/h4l-small
```

It prints the name and URL of the run it started. The run's output is the
agent's last message, or the notice that the workspace has reached its
`tokens`. While the run's pod exists, its log follows the agent:

```bash
uv run --project docker/agentic-analysis --frozen flyte --config workflows/agentic-analysis/config.yaml get logs <run>
```

A reply starts another run in the same workspace and reaches the agent
verbatim. It names the analysis the workspace was made with, and `<workspace>`
is the name of the first run; JFC's gate expects `APPROVE`, `ITERATE: ...` or
`REGRESS(N)`:

```bash
uv run --project docker/agentic-analysis --frozen python workflows/agentic-analysis/agentic_analysis.py workflows/agentic-analysis/analyses/h4l-small <workspace> "APPROVE"
```

Without a reply, a run on an existing workspace continues where the last one
stopped, which is how a failed run is picked up.

From a machine with `kubectl` access to `cms`, `browse.sh` opens the
`agentic-analysis` volume read-only at `/agentic-analysis` in a pod of its own:
a shell, a given command, or `serve [port]` for its files over HTTP on
localhost.

## Adding an analysis

A directory `analyses/<name>` with an `analysis.toml`:

| Key      | What it is                                                                                                                    |
| -------- | ----------------------------------------------------------------------------------------------------------------------------- |
| `name`   | Names the analysis in JFC, in run names and in its data directory                                                             |
| `kind`   | The JFC analysis type: `measurement` or `search`                                                                              |
| `repo`   | Optional: repository holding the physics prompt, `prompt.md`, at its top level; without it, the analysis's directory holds it |
| `ref`    | With `repo`: the commit of it to use                                                                                          |
| `files`  | Optional: files and directories beside the prompt to put beside it in the analysis; text files, without `repo`                |
| `data`   | Optional: URL of a tarball that unpacks to `data/`, linked into the analysis as `data/`                                       |
| `scope`  | Optional: text put after the prompt, to narrow what the analysis does                                                         |
| `tokens` | Optional: the most tokens, cached context included, that the runs in one workspace may process; a run that reaches it stops   |

## Tuning

All in `agentic_analysis.py`:

- `JFC` pins the methodology by commit.
- `MODEL` and `PERMISSION_MODE` are passed to Claude Code; `FACILITY` is what
  every agent reads.
- `env` holds what a task pod reserves; `RETRIES` and the timeout of `advance`
  bound a run, `LIMITED` paces its waits on a usage limit, and `COMMAND_HOURS`
  bounds a foreground command of the agent's.
