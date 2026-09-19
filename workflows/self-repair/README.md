# self-repair

An always-on [Flyte 2](https://www.union.ai/docs/v2/flyte/) workflow that
watches the facility's logs in Loki, has a coding agent analyze every new
error, and opens a draft pull request against this repository when the fix is
a code change here. The control plane is [`apps/flyte`](../../apps/flyte); the
launcher, task pods and secrets are [`apps/self-repair`](../../apps/self-repair),
which also covers running a tick by hand; the task image is
[`docker/self-repair`](../../docker/self-repair).

| File             | What it is                                                                      |
| ---------------- | ------------------------------------------------------------------------------- |
| `self_repair.py` | The tasks, the environment and the launcher (`__main__`)                        |
| `triage.py`      | Loki query, error fingerprints, username redaction, verdict parsing, GitHub API |
| `prompts.py`     | What the agent is told, for analysis and for the fix                            |
| `genai_proxy.py` | Re-framing proxy on 127.0.0.1 between opencode and GenAI Studio                 |
| `config.yaml`    | Where `flyte` finds the control plane                                           |

## Tasks

| Task      | Runs                                                                                                | Cached                                                      |
| --------- | --------------------------------------------------------------------------------------------------- | ----------------------------------------------------------- |
| `triage`  | One per tick: `watch`, then `analyze` per incident, then `fix`, each started as a run of its own    | no                                                          |
| `watch`   | Two Loki queries, infrastructure then user workloads, for error lines from the watched workloads    | no                                                          |
| `dedupe`  | One model call: the incidents grouped by root cause; a failed call leaves every incident alone      | no                                                          |
| `analyze` | opencode, read-only, in a fresh checkout of `main`: is this fixable by a change in this repository? | yes, on the incident key (a recurring error is judged once) |
| `fix`     | opencode with edit rights on a branch `self-repair-<fingerprint>`; commit, push, draft PR           | no (an open PR for the branch is returned as is)            |

`triage` starts the others with `flyte.run` under names of their own
(`run_name` in `self_repair.py`), so runs and pods say what they are and a
cache hit on `analyze` is visible as such. `max_incidents` caps fresh analyses
per tick; a cache hit is free and does not count, so known errors at the top
of the list never starve the ones below. A failed analysis is logged and
skipped; it does not end the tick.

Only pods this repository deploys or configures are read, through a prefix
allowlist in the Loki selector: `WATCHED_WORKLOADS` in `triage.py` for what
Flux deploys from `apps/`, and `USER_WORKLOADS` for user sessions and user Dask
clusters, whose image, start hooks, pixi environments and worker configuration
come from here even though the code inside is the user's. Their incidents rank
after the infrastructure's, so they only take analysis budget the
infrastructure left. `IGNORED_WORKLOADS` lists what is deployed from here but
not debugged by this workflow. The workflow's own pods are not read. A new app
in `apps/` needs its pod prefix added to `WATCHED_WORKLOADS`.

Incidents are deduplicated in two layers. First structurally: logfmt and JSON
lines are keyed on their level and message fields, so field order and extra
fields do not split one condition, and a Python traceback is keyed on its
exception line rather than its file paths. Then `dedupe`, one model call per
tick, groups the remaining incidents by root cause; `analyze` sees one
representative per group carrying the group's counts, and the report names the
group and its size.

An incident is `(container, workload, normalized message)`: timestamps, ids,
addresses and numbers are replaced before hashing, so the same error from
every replica over every tick is one incident. Usernames are redacted from
pod names, paths and `user=` fields before anything reaches a PR.

The agent is [opencode](https://opencode.ai) on
[Purdue GenAI Studio](https://docs.rcac.purdue.edu/services/genai/). A tick
starts by probing `MODELS` in order and runs every task on the first that
answers; when none does, the tick ends there, before `watch`. Analysis runs
with edit and bash denied; the fix runs with edit allowed and the git commands
that commit or move branches denied — the task commits and pushes. Both
prompts carry the lines logged by the same container just before and after the
first sample, so a traceback split over Loki lines is seen whole.

A PR is opened only when the verdict is fixable at confidence ≥
`MIN_CONFIDENCE`, at most `max_fixes` per tick, and always as a draft, titled
`[self-repair] …` and labelled `self-repair`. A change is never proposed when it
touches `PROTECTED_PATHS` or a lock file, when a Python file fails a pyflakes
check, when it only lowers a log level or rewords a message, when it deletes
more error handling than it puts back, or when it fails the suite `check-unit`
runs (an environment that cannot be built skips that gate). What the agent is
told is in `prompts.py`.

opencode does not talk to GenAI Studio directly: `genai_proxy.py` runs on
127.0.0.1 in the task pod and hands opencode well-formed responses and
rate-limit signals; its docstring has the details and GenAI Studio's limits.

## At a glance

The `triage` run has a report in the console (the Report tab of run
`self-repair-triage-<tick>`): the headline is how many of the analyzed
incidents are fixable in this repository, followed by one line per incident
with verdict, confidence, title, component, PR and reason, fixable ones on
top. It is updated after `watch`, after the analyses, and after the fixes.
The same headline is the `VERDICTS:` line of the triage log, and the counts
are the run's `Summary` output.

## Logs

Every task narrates what it does: the Loki window and the top incidents, the
clone, each opencode tool call and reply as it happens, the verdict and its
reason, changed files, push and PR. `triage` logs each run it starts, its
phase, duration and cache status. opencode's own log is relayed too: its ERROR
and WARN lines appear as `opencode …`, which is where a provider failure such
as a rate limit shows up. A heartbeat reports every minute of silence with the
agent's last action; a provider error followed by silence ends the session,
and a rate limit is retried once after a pause. Read them with
`kubectl -n cms logs <pod>` or `flyte get logs <run>`.

## Running

From any pod with the image and the code:

```bash
cd /workflow
python self_repair.py
flyte --config config.yaml get run
flyte --config config.yaml get logs <run-name>
```

## Metrics

`triage` pushes one sample set per tick to the AF Prometheus pushgateway
(`apps/monitoring/prometheus`, scrape job `self-repair`); `METRICS` in
`triage.py` lists the series. The private Grafana shows them on
"Purdue AF Self-Repair" (`apps/monitoring/grafana/dashboards/self-repair.json`).
A tick that cannot reach the gateway logs it and goes on.

## Tuning

- `MODELS` (env `SELF_REPAIR_MODELS`, comma-separated): `genai/<id>` for any
  GenAI Studio model listed in `PROVIDERS`, or any `provider/model` opencode
  knows (an `opencode/*` Zen model needs the `self-repair-opencode` Secret and
  is not probed), in the order to try them.
- `triage` inputs (`window_minutes`, `max_incidents`, `max_fixes`) have defaults
  in the task signature; the launcher passes only `trigger_time`.
- To re-judge every known error, add a `salt` to the `flyte.Cache` of `analyze`.
