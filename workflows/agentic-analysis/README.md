# agentic-analysis

Runs physics analyses unattended on [Flyte 2](https://www.union.ai/docs/v2/flyte/)
with the [JFC](https://github.com/jfc-mit/jfc) methodology:
[OpenCode](https://opencode.ai), following the JFC specification on the models
of the [LLM proxy](../../apps/agentic-analysis/README.md#llm-proxy), takes an
analysis from its prompt to a typeset analysis note. The control plane is
[`apps/flyte`](../../apps/flyte); the task pods, the volume they work in, their
workload account and the LLM proxy are
[`apps/agentic-analysis`](../../apps/agentic-analysis); the task image is
[`docker/agentic-analysis`](../../docker/agentic-analysis). An analysis is a
directory under [`analyses/`](analyses) with an `analysis.toml`, such as
[`h4l`](analyses/h4l) or [`hmm`](analyses/hmm).

| File                  | What it is                                                   |
| --------------------- | ------------------------------------------------------------ |
| `agentic_analysis.py` | The task, its environment and the launcher (`__main__`)      |
| `af_opencode.js`      | The OpenCode plugin every turn runs with                     |
| `af_profiling.py`     | The profiler hook installed into each analysis's environment |
| `af_dask.py`          | The Dask cluster helper installed into the same environment  |
| `af_ray.py`           | The Ray jobs helper installed into the same environment      |
| `browse.sh`           | Opens the `agentic-analysis` volume read-only                |
| `config.yaml`         | Where `flyte` finds the control plane                        |
| `analyses/`           | The analyses                                                 |

A run is one `advance` task: the setup if the workspace is new, then one
headless OpenCode turn. It ends when the agent ends its turn, either with the
analysis note finished or with a message for a person. JFC stops that way to
show the draft note before it looks at the full data. A turn whose last reply
ran out of output tokens is resumed, up to `RESUMES` times.

Before each turn the runner configures OpenCode afresh from the proxy's
`/config`, in a read-only directory of the pod's own:

- one provider, the proxy, with a model per role of the proxy's;
- the orchestrator, and a subagent per JFC role on its role's model (`AGENTS`),
  which only the orchestrator starts;
- permissions under which nothing asks and no agent writes OpenCode's
  configuration (`PERMISSION`); before each turn, what OpenCode would load
  over it or over JFC's `CLAUDE.md` files (`CONFIGS`), from the analysis or
  `$HOME`, goes to the workspace's `opencode/quarantine`;
- `FACILITY`, which the agent and every subagent read beside the analysis's
  `CLAUDE.md`: how this facility runs subagents, bounds commands, keeps an
  analysis's files, splits and reviews figures and treats PDFs, reads CMS data through XCache with the grid proxy the task pods
  hold, processes it on
  [Dask Gateway](../../apps/dask-gateway) with `af_dask.client()`, and trains
  on GPUs through [Ray](../../apps/ray-train) with `af_ray.client()`. Those
  clusters are the
  [workload account](../../apps/dask-gateway/README.md#workload-accounts)
  `agentic-analysis`'s;
- the plugin `af_opencode.js`. It holds subagents to `SEATS` at once, the
  others waiting for a seat; sends the proxy each request's session, agent and
  parent session; keeps each session within the images its role takes,
  refusing a `read` beyond them and dropping those other tools return, and
  refuses to `read` a PDF; tells the orchestrator when a subagent's last reply
  ran out of output tokens, and hands it the reply a subagent ended before
  OpenCode compacted its session and had it go on; gives commands the pod's
  own XDG directories and holds their timeout to `COMMAND_HOURS`; and records
  each request and command as it starts and what the subagents do, which the
  task's log follows.

A turn whose plugin has not loaded within `LOADED` seconds, or that says
nothing for `STALL` seconds, is ended, and its attempt fails. What a turn
started ends with it, the sessions its commands run in included.

A workspace is `/agentic-analysis/<name of its first run>`: the JFC checkout
with the analysis scaffolded in it, its pixi environment and git history, the
agent's sessions and the plugin's records in `opencode/`, the replies it was
given, and the last run's output in `said.md`. An analysis's data is fetched
once per URL under `/agentic-analysis/data`. A retried task resumes the
session.

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
agent's last message. While the run's pod exists, its log follows the agent:

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

| Key     | What it is                                                                                                                    |
| ------- | ----------------------------------------------------------------------------------------------------------------------------- |
| `name`  | Names the analysis in JFC, in run names and in its data directory                                                             |
| `kind`  | The JFC analysis type: `measurement` or `search`                                                                              |
| `repo`  | Optional: repository holding the physics prompt, `prompt.md`, at its top level; without it, the analysis's directory holds it |
| `ref`   | With `repo`: the commit of it to use                                                                                          |
| `files` | Optional: files and directories beside the prompt to put beside it in the analysis; text files, without `repo`                |
| `data`  | Optional: URL of a tarball that unpacks to `data/`, linked into the analysis as `data/`                                       |
| `scope` | Optional: text put after the prompt, to narrow what the analysis does                                                         |

## Tuning

All in `agentic_analysis.py`:

- `JFC` pins the methodology by commit.
- `AGENTS` puts each JFC role on a role of the proxy's; `PERMISSION`, `SEATS`
  and `FACILITY` are what every agent works under.
- `env` holds what a task pod reserves; `RETRIES` and the timeout of `advance`
  bound a run, `RESUMES` its resumes of a cut-off reply, `COMMAND_HOURS` a
  foreground command of the agent's and `COMMAND_MINUTES` one that sets no
  timeout, and `LOADED` and `STALL` a turn's start and its silences.
