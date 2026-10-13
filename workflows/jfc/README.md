# jfc

Runs [JFC](https://github.com/jfc-mit/jfc) analyses unattended on
[Flyte 2](https://www.union.ai/docs/v2/flyte/): Claude Code, following the JFC
specification, takes a physics analysis from its prompt to a typeset analysis
note. The control plane is [`apps/flyte`](../../apps/flyte); the task pods,
their directory on `/work` and the model credentials are
[`apps/jfc`](../../apps/jfc); the task image is
[`docker/self-repair`](../../docker/self-repair). An analysis is a directory
with an `analysis.toml`, such as [`workflows/jfc-h4l`](../jfc-h4l).

| File          | What it is                                              |
| ------------- | ------------------------------------------------------- |
| `jfc.py`      | The task, its environment and the launcher (`__main__`) |
| `config.yaml` | Where `flyte` finds the control plane                   |

A run is one `advance` task: the setup if the workspace is new, then one
headless Claude Code turn. It ends when the agent ends its turn, either with
the analysis note finished or with a message for a person. JFC stops that way
to show the draft note before it looks at the full data.

A workspace is `/work/projects/jfc/<name of its first run>`: the JFC checkout
with the analysis scaffolded in it, its pixi environment and git history, the
agent's session, the replies it was given, and its last message in `said.md`.
An analysis's data is fetched once into `/work/projects/jfc/data/<analysis>`. A
retried task resumes the session.

## Running

From an AF session, in a checkout of this repository, with the directory of an
analysis:

```bash
uv run --project docker/self-repair --frozen python workflows/jfc/jfc.py workflows/jfc-h4l
```

It prints the name of the run it started. The agent's last message is the
run's output and `said.md` in the workspace. While the run's pod exists, its
log follows the agent:

```bash
uv run --project docker/self-repair --frozen flyte --config workflows/jfc/config.yaml get logs <run>
```

A reply starts another run in the same workspace and reaches the agent
verbatim. `<workspace>` is the name of the first run; JFC's gate expects
`APPROVE`, `ITERATE: ...` or `REGRESS(N)`:

```bash
uv run --project docker/self-repair --frozen python workflows/jfc/jfc.py workflows/jfc-h4l <workspace> "APPROVE"
```

Without a reply, a run on an existing workspace continues where the last one
stopped, which is how a failed run is picked up.

## Adding an analysis

A directory `workflows/jfc-<name>` with an `analysis.toml`:

| Key     | What it is                                                                              |
| ------- | --------------------------------------------------------------------------------------- |
| `name`  | Names the analysis in JFC, in run names and under `data/`                               |
| `kind`  | The JFC analysis type: `measurement` or `search`                                        |
| `repo`  | Repository holding the physics prompt, `prompt.md`, at its top level                    |
| `ref`   | The commit of it to use                                                                 |
| `files` | Optional: files and directories at its top level to put beside the prompt               |
| `data`  | Optional: URL of a tarball that unpacks to `data/`, linked into the analysis as `data/` |

## Tuning

All in `jfc.py`:

- `JFC` pins the specification by commit.
- `MODEL` and `PERMISSION_MODE` are passed to Claude Code.
- `env` holds what a task pod reserves; `RETRIES` and the timeout of `advance`
  bound a run.
