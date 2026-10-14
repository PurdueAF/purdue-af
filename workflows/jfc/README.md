# jfc

Runs [JFC](https://github.com/jfc-mit/jfc) analyses unattended on
[Flyte 2](https://www.union.ai/docs/v2/flyte/): Claude Code, following the JFC
specification, takes a physics analysis from its prompt to a typeset analysis
note. The control plane is [`apps/flyte`](../../apps/flyte); the task pods,
their directory on `/work` and the model credentials are
[`apps/jfc`](../../apps/jfc); the task image is
[`docker/self-repair`](../../docker/self-repair). An analysis is a directory
with an `analysis.toml`, such as [`workflows/jfc-h4l`](../jfc-h4l).

| File               | What it is                                                   |
| ------------------ | ------------------------------------------------------------ |
| `jfc.py`           | The task, its environment and the launcher (`__main__`)      |
| `jfc_profiling.py` | The profiler hook installed into each analysis's environment |
| `jfc_dask.py`      | The Dask cluster helper installed into the same environment  |
| `config.yaml`      | Where `flyte` finds the control plane                        |

A run is one `advance` task: the setup if the workspace is new, then one
headless Claude Code turn. It ends when the agent ends its turn, either with
the analysis note finished or with a message for a person. JFC stops that way
to show the draft note before it looks at the full data.

Before each turn the runner writes `FACILITY` as Claude Code's user memory,
which the agent and every subagent read: start independent subagents
together, and run every pass over the events on
[Dask Gateway](../../apps/dask-gateway) through `jfc_dask.client()`, in place
of the local processes and SLURM the methodology names. That returns a client
of one cluster per analysis, whose workers run the analysis's environment and
scale with the work. The cluster belongs to the owner of the Hub token in
[`jfc-claude`](../../apps/jfc/README.md#secrets).

A workspace is `/work/projects/jfc/<name of its first run>`: the JFC checkout
with the analysis scaffolded in it, its pixi environment and git history, the
agent's session, the replies it was given, the tokens its runs have processed,
and the last run's output in `said.md`.
An analysis's data is fetched once per URL under `/work/projects/jfc/data`. A
retried task resumes the session.

Every Python process in an analysis's environment, its Dask cluster's
included, sends CPU profiles to [Pyroscope](../../apps/monitoring/pyroscope),
under `service_name="jfc"` and tagged with its `analysis`, `workspace` and
`script`. The setup keeps that environment's Python within the versions
`pyroscope-io` ships wheels for, adds `PACKAGES`, and installs both helpers,
with a `.pth` file that starts `jfc_profiling`.

## Running

From an AF session, in a checkout of this repository, with the directory of an
analysis:

```bash
uv run --project docker/self-repair --frozen python workflows/jfc/jfc.py workflows/jfc-h4l-small
```

It prints the name and URL of the run it started. The run's output is the
agent's last message, or the notice that the workspace has reached its
`tokens`. While the run's pod exists, its log follows the agent:

```bash
uv run --project docker/self-repair --frozen flyte --config workflows/jfc/config.yaml get logs <run>
```

A reply starts another run in the same workspace and reaches the agent
verbatim. It names the analysis the workspace was made with, and `<workspace>`
is the name of the first run; JFC's gate expects `APPROVE`, `ITERATE: ...` or
`REGRESS(N)`:

```bash
uv run --project docker/self-repair --frozen python workflows/jfc/jfc.py workflows/jfc-h4l-small <workspace> "APPROVE"
```

Without a reply, a run on an existing workspace continues where the last one
stopped, which is how a failed run is picked up.

## Adding an analysis

A directory `workflows/jfc-<name>` with an `analysis.toml`:

| Key      | What it is                                                                                                                  |
| -------- | --------------------------------------------------------------------------------------------------------------------------- |
| `name`   | Names the analysis in JFC, in run names and in its data directory                                                           |
| `kind`   | The JFC analysis type: `measurement` or `search`                                                                            |
| `repo`   | Repository holding the physics prompt, `prompt.md`, at its top level                                                        |
| `ref`    | The commit of it to use                                                                                                     |
| `files`  | Optional: files and directories at its top level to put beside the prompt                                                   |
| `data`   | Optional: URL of a tarball that unpacks to `data/`, linked into the analysis as `data/`                                     |
| `scope`  | Optional: text put after the prompt, to narrow what the analysis does                                                       |
| `tokens` | Optional: the most tokens, cached context included, that the runs in one workspace may process; a run that reaches it stops |

## Tuning

All in `jfc.py`:

- `JFC` pins the specification by commit.
- `MODEL` and `PERMISSION_MODE` are passed to Claude Code; `FACILITY` is what
  every agent reads.
- `env` holds what a task pod reserves; `RETRIES` and the timeout of `advance`
  bound a run.
