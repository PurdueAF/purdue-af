# self-repair

An always-on [Flyte 2](https://www.union.ai/docs/v2/flyte/) workflow that
watches the facility's logs in Loki, has a coding agent analyze every new
error, and opens a draft pull request against this repository when the fix is
a code change here. The control plane is [`apps/flyte`](../../apps/flyte); the
cluster side (task pods, secrets, the deploy Job) is
[`apps/self-repair`](../../apps/self-repair); the task image is
[`docker/self-repair`](../../docker/self-repair).

| File             | What it is                                                                      |
| ---------------- | ------------------------------------------------------------------------------- |
| `self_repair.py` | The tasks, the environment and the launcher (`__main__`)                        |
| `triage.py`      | Loki query, error fingerprints, username redaction, verdict parsing, GitHub API |
| `prompts.py`     | What the agent is told, for analysis and for the fix                            |
| `config.yaml`    | Where `flyte` finds the control plane                                           |

## Tasks

| Task      | Runs                                                                                                | Cached                                                      |
| --------- | --------------------------------------------------------------------------------------------------- | ----------------------------------------------------------- |
| `triage`  | One per tick: `watch`, then `analyze` per incident, then `fix`, each started as a run of its own    | no                                                          |
| `watch`   | One Loki query for `error\|exception\|traceback\|fatal\|panic` from the watched workloads in `cms`  | no                                                          |
| `analyze` | opencode, read-only, in a fresh checkout of `main`: is this fixable by a change in this repository? | yes, on the incident key (a recurring error is judged once) |
| `fix`     | opencode with edit rights on a branch `self-repair-<fingerprint>`; commit, push, draft PR           | no (an open PR for the branch is returned as is)            |

`triage` starts the others with `flyte.run` under names of its own —
`self-repair-watch-<tick>`, `self-repair-analyze-<tick>-<fp>`,
`self-repair-fix-<tick>-<fp>`, the tick being the launch minute in base36 and
`fp` four characters of the fingerprint, within Flyte's 30-character cap on
run names — so runs and pods say what they are,
and a cache hit on `analyze` is visible as such. `max_incidents` caps fresh analyses per tick;
a cache hit is free and does not count, so known errors at the top of the
list never starve the ones below. A failed analysis is logged and skipped; it
does not end the tick.

Only pods this repository deploys or configures are read, through a prefix
allowlist in the Loki selector: `WATCHED_WORKLOADS` in `triage.py` for what
Flux deploys from `apps/`, and `USER_WORKLOADS` for user sessions
(`purdue-af-<id>`) and user Dask clusters, whose image, start hooks, pixi
environments and worker configuration come from here even though the code
inside is the user's. Their incidents rank after the infrastructure's, so they
only take analysis budget the infrastructure left. The workflow's own pods are not
read either: their logs quote every error they analyze. Anything in the
namespace without a manifest here is not read. A new app in `apps/` needs its pod prefix
added to the list.

An incident is `(container, workload, normalized message)`: timestamps, ids,
addresses and numbers are replaced before hashing, so the same error from
every replica over every tick is one incident. Usernames are redacted from
pod names, paths and `user=` fields before anything reaches a PR.

The agent is [opencode](https://opencode.ai) with a free OpenCode Zen model
(`MODEL` in `self_repair.py`). Analysis runs with edit and bash denied; the fix
runs with edit allowed and `git push`/`commit`/`checkout`/`reset` denied — the
task commits and pushes. A PR is opened only when the verdict is fixable at
confidence ≥ `MIN_CONFIDENCE`, at most `max_fixes` per tick, and always as a
draft.

Guardrails and the definition of "fixable here" are in `prompts.py`.

## Logs

Every task narrates what it does: the Loki window and the top incidents, the
clone, each opencode tool call and reply as it happens, the verdict and its
reason, changed files, push and PR. `triage` logs each run it starts, its
phase, duration and cache status. `kubectl -n cms logs <pod>` or
`flyte get logs <run>`.

## Running

The launcher is a CronJob ([`apps/self-repair`](../../apps/self-repair)),
suspended while testing; start a tick by hand with

```bash
kubectl -n cms create job --from=cronjob/self-repair self-repair-manual-$(date +%s)
```

or from any pod with the image and the code:

```bash
cd /workflow
python self_repair.py
flyte --config config.yaml get run
flyte --config config.yaml get logs <run-name>
```

## Tuning

- `MODEL`: any `provider/model` opencode knows; a paid Zen model needs the
  `self-repair-opencode` Secret.
- `triage` inputs (`window_minutes`, `max_incidents`, `max_fixes`) have defaults
  in the task signature; the launcher passes only `trigger_time`.
- To re-judge every known error, add a `salt` to the `flyte.Cache` of `analyze`.
