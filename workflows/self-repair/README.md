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
| `genai_proxy.py` | Re-framing proxy on 127.0.0.1 between opencode and GenAI Studio (see below)     |
| `config.yaml`    | Where `flyte` finds the control plane                                           |

## Tasks

| Task      | Runs                                                                                                                                                        | Cached                                                      |
| --------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------- |
| `triage`  | One per tick: `watch`, then `analyze` per incident, then `fix`, each started as a run of its own                                                            | no                                                          |
| `watch`   | Two Loki queries (infrastructure, then user workloads, 5000 lines each) for `error\|exception\|traceback\|fatal\|panic` from the watched workloads in `cms` | no                                                          |
| `dedupe`  | One model call: the incidents grouped by root cause; a failed call leaves every incident alone                                                              | no                                                          |
| `analyze` | opencode, read-only, in a fresh checkout of `main`: is this fixable by a change in this repository?                                                         | yes, on the incident key (a recurring error is judged once) |
| `fix`     | opencode with edit rights on a branch `self-repair-<fingerprint>`; commit, push, draft PR                                                                   | no (an open PR for the branch is returned as is)            |

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
only take analysis budget the infrastructure left. `IGNORED_WORKLOADS` lists what is
deployed from here but not debugged by this workflow for now: the SONIC stack
(`supersonic-*`, `sonic-ray`, `kuberay-operator`) and the interLink nodes. The workflow's own pods are not
read either: their logs quote every error they analyze. Anything in the
namespace without a manifest here is not read. A new app in `apps/` needs its pod prefix
added to the list.

Incidents are deduplicated in two layers. First structurally: logfmt and JSON
lines are keyed on their level and message fields, so field order and extra
fields (`ingress=`, `servicePort=`) do not split one condition, and a Python
traceback is keyed on its exception line rather than its file paths. Then
`dedupe`, one model call per tick, groups the remaining incidents by root
cause (a service missing, then its endpoints, then the route failing are one
group); `analyze` sees one representative per group carrying the group's
counts, and the report names the group and its size.

An incident is `(container, workload, normalized message)`: timestamps, ids,
addresses and numbers are replaced before hashing, so the same error from
every replica over every tick is one incident. Usernames are redacted from
pod names, paths and `user=` fields before anything reaches a PR.

The agent is [opencode](https://opencode.ai) on
[Purdue GenAI Studio](https://docs.rcac.purdue.edu/services/genai/)
(`MODELS` and `PROVIDERS` in `self_repair.py`; the key is the
`self-repair-genai` Secret). A tick starts by probing the models in order,
`gemma4:26b-a4b`, `gpt-oss:120b`, `llama4:latest`, with a tiny completion
and a 30 s deadline, and runs every task on the first that answers; when none
does, the tick ends there, before `watch`. GenAI Studio allows 60 requests a
minute per user and about 10 concurrent calls per model, which is what sizes
`max_incidents`. Analysis runs with edit and bash denied; the fix
runs with edit allowed and `git push`/`commit`/`checkout`/`reset` denied — the
task commits and pushes. Both prompts carry the lines
logged by the same container within five seconds of the first sample, so a
traceback split over Loki lines is seen whole. A PR is opened only when the verdict is fixable at
confidence ≥ `MIN_CONFIDENCE`, at most `max_fixes` per tick, and always as a
draft, titled `[self-repair] …` and labelled `self-repair` so it is never mistaken
for a human's. The agent may not edit `docker/dask-gateway-server` (an upstream
fork carried verbatim), `pixi/`, `deploy/` or lock files, a change touching
them is never proposed, and a Python change must pass a pyflakes check
(undefined names, unused imports) regardless of the repository's lint
exclusions. A change that only lowers a log level or rewords a message is
not a fix and is never proposed.

Guardrails and the definition of "fixable here" are in `prompts.py`.

opencode does not talk to GenAI Studio directly. GenAI Studio's streaming
responses end by closing the connection without the chunked-encoding
terminator; Node's fetch, which opencode uses, reports that as a reset, the
adapter retries the step, and every tool call of the step runs again — the
same `read` five times, then a hard failure. `genai_proxy.py` runs on
127.0.0.1 in the task pod, reads the upstream leniently, and hands opencode
well-formed responses; it also turns GenAI Studio's rate-limit signal, a JSON
`null` body, into an HTTP 429. The provider's `baseURL` is set to the proxy
per session.

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
phase, duration and cache status. opencode's own log (`~/.local/share/opencode/log/opencode.log` in the
pod) is relayed too: its ERROR and WARN lines appear as `opencode …`, which
is where a provider failure such as a rate limit shows up, since opencode 1.18
neither retries nor exits on one. A heartbeat reports every minute of silence
with the agent's last action; a provider error followed by two minutes of
silence ends the session, and a rate limit is retried once after a pause.
`kubectl -n cms logs <pod>` or
`flyte get logs <run>`.

## Running

The launcher is a CronJob ([`apps/self-repair`](../../apps/self-repair)),
hourly, each tick reading a little more than an hour of logs; start an extra
tick by hand with

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

## Metrics

`triage` pushes one sample set per tick to the AF Prometheus pushgateway
(`apps/monitoring/prometheus`, scrape job `self-repair`): `self_repair_*_total`
counters carried across ticks (ticks by outcome, analyses by result, fixable
incidents, pull requests, fix failures, probes by model) and
`self_repair_last_tick_*` / `self_repair_model_*` gauges for the newest tick
(`METRICS` in `triage.py` lists them). The private Grafana shows them on
"Purdue AF Self-Repair" (`apps/monitoring/grafana/dashboards/self-repair.json`).
A tick that cannot reach the gateway logs it and goes on.

## Tuning

- `MODELS` (env `SELF_REPAIR_MODELS`, comma-separated): `genai/<id>` for any
  GenAI Studio model listed in `PROVIDERS`, or any `provider/model` opencode
  knows (an `opencode/*` Zen model needs the `self-repair-opencode` Secret and
  is not probed), in the order to try them.
- `triage` inputs (`window_minutes`, `max_incidents`, `max_fixes`) have defaults
  in the task signature; the launcher passes only `trigger_time`.
- The agents get the same platform context as the agents in an AF session:
  `docker/purdue-af/agents/platform-context.md`, mounted at
  `/opt/purdue-af/agents/` and named in opencode's `instructions`.
- To re-judge every known error, add a `salt` to the `flyte.Cache` of `analyze`.
