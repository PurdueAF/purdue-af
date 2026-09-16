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
| `self_repair.py` | The tasks, the environment and the cron trigger                                 |
| `triage.py`      | Loki query, error fingerprints, username redaction, verdict parsing, GitHub API |
| `prompts.py`     | What the agent is told, for analysis and for the fix                            |
| `config.yaml`    | Where `flyte` finds the control plane                                           |

## Tasks

| Task      | Runs                                                                                                             | Cached                                                      |
| --------- | ---------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------- |
| `triage`  | Every 15 minutes (`flyte.Cron`), over the previous 20 minutes: `watch`, then `analyze` each incident, then `fix` | no                                                          |
| `watch`   | One Loki query for `error\|exception\|traceback\|fatal\|panic` in `cms`, grouped into incidents                  | no                                                          |
| `analyze` | opencode, read-only, in a fresh checkout of `main`: is this fixable by a change in this repository?              | yes, on the incident key (a recurring error is judged once) |
| `fix`     | opencode with edit rights on a branch `self-repair-<fingerprint>`; commit, push, draft PR                        | no (an open PR for the branch is returned as is)            |

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

## Running

Flux deploys the trigger; nothing else is needed. To trigger one run by hand
from a pod that has the image (the deploy Job's, for instance):

```bash
cd /workflow
python self_repair.py
flyte --config config.yaml get run
flyte --config config.yaml get logs <run-name>
```

To pause:

```bash
flyte --config config.yaml update trigger every-15-minutes self-repair.triage --deactivate -p self-repair -d development
```

## Tuning

- `MODEL`: any `provider/model` opencode knows; a paid Zen model needs the
  `self-repair-opencode` Secret.
- `triage` inputs (`window_minutes`, `max_incidents`, `max_fixes`) are trigger
  defaults; override them in the `Trigger.inputs`.
- To re-judge every known error, add a `salt` to the `flyte.Cache` of `analyze`.
