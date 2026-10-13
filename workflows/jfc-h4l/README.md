# jfc-h4l

A [Flyte 2](https://www.union.ai/docs/v2/flyte/) workflow that runs the H→4ℓ
exercise of the
[JFC tutorial](https://github.com/jfc-mit/jfc-tutorial-nrp-clariphy)
unattended: Claude Code, following the [JFC](https://github.com/jfc-mit/jfc)
specification, takes a Higgs boson mass and signal-strength measurement from
the physics prompt to a typeset analysis note. The control plane is
[`apps/flyte`](../../apps/flyte); the task pods, their directory on `/work` and
the model credentials are [`apps/jfc-h4l`](../../apps/jfc-h4l); the task image
is [`docker/self-repair`](../../docker/self-repair).

| File          | What it is                                              |
| ------------- | ------------------------------------------------------- |
| `jfc_h4l.py`  | The task, its environment and the launcher (`__main__`) |
| `config.yaml` | Where `flyte` finds the control plane                   |

A run is one `advance` task: the tutorial's setup if the workspace is new, then
one headless Claude Code turn. It ends when the agent ends its turn, either
with the analysis note finished or with a message for a person. JFC stops that
way to show the draft note before it looks at the full data.

A workspace is `/work/projects/jfc-h4l/<name of its first run>`: the JFC and
exercise checkouts, the analysis with its pixi environment and git history, the
agent's session, the replies it was given, and its last message in `said.md`.
The tutorial's ntuples are fetched once into `/work/projects/jfc-h4l/data`. A
retried task resumes the session.

## Running

From an AF session, in a checkout of this repository:

```bash
uv run --project docker/self-repair --frozen python workflows/jfc-h4l/jfc_h4l.py
```

It prints the name of the run it started. The agent's last message is the
run's output and `said.md` in the workspace. While the run's pod exists, its
log follows the agent:

```bash
uv run --project docker/self-repair --frozen flyte --config workflows/jfc-h4l/config.yaml get logs <run>
```

A reply starts another run in the same workspace and reaches the agent
verbatim. `<workspace>` is the name of the first run; JFC's gate expects
`APPROVE`, `ITERATE: ...` or `REGRESS(N)`:

```bash
uv run --project docker/self-repair --frozen python workflows/jfc-h4l/jfc_h4l.py <workspace> "APPROVE"
```

Without a reply, a run on an existing workspace continues where the last one
stopped, which is how a failed run is picked up.

## Tuning

All in `jfc_h4l.py`:

- `JFC` and `EXERCISE` pin the specification and the exercise by commit; `DATA`
  is where the ntuples are fetched from.
- `MODEL` and `PERMISSION_MODE` are passed to Claude Code.
- `env` holds what a task pod reserves; `RETRIES` and the timeout of `advance`
  bound a run.
