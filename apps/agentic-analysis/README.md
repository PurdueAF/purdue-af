# agentic-analysis

Cluster side of the [`agentic-analysis`](../../workflows/agentic-analysis)
workflow: its task pods, the volume they work in, which
[`browse.sh`](../../workflows/agentic-analysis/browse.sh) opens, its
[workload account](../dask-gateway/README.md#workload-accounts), whose Dask and
Ray clusters run the analyses' work on that volume, and an
[LLM proxy](#llm-proxy) for NRP's models. Runs are
started by hand ([Running](../../workflows/agentic-analysis/README.md#running))
on the Flyte control plane ([`apps/flyte`](../flyte)).

| File                    | What it is                                                                                                                                                                            |
| ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `account.yaml`          | The `agentic-analysis` [workload account](../dask-gateway/README.md#workload-accounts): the UID and GID of its Dask and Ray clusters, and its token's SHA-256                         |
| `llm_proxy.py`          | The [LLM proxy](#llm-proxy)                                                                                                                                                           |
| `models.toml`           | The proxy's roles, models and limits                                                                                                                                                  |
| `llm-proxy.yaml`        | The proxy's Deployment and Service, `agentic-analysis-llm-proxy`, running both files from the ConfigMap Flux makes of them                                                            |
| `podtemplate.yaml`      | Pod spec of every task pod: AF node placement, the account's user, the LLM proxy and its client token, the account's token, where profiles go, the Dask Gateway, the facility's grid proxy and CVMFS, the volume at `/agentic-analysis` |
| `secret-account.yaml`   | The `agentic-analysis-account` Secret ([Secrets](#secrets))                                                                                                                           |
| `secret-llm-proxy.yaml` | The `agentic-analysis-llm-proxy` Secret ([Secrets](#secrets))                                                                                                                         |
| `secret-nrp.yaml`       | The `agentic-analysis-nrp` Secret ([Secrets](#secrets))                                                                                                                               |
| `volume.yaml`           | The `agentic-analysis` volume, which Flux never prunes                                                                                                                                |
| `workspace.yaml`        | The Job that hands the volume's root to the account's user                                                                                                                            |

## Secrets

| Secret                       | Holds                                                                                                 |
| ---------------------------- | ----------------------------------------------------------------------------------------------------- |
| `agentic-analysis-account`   | `token`: the token of the workload account `account.yaml` declares                                    |
| `agentic-analysis-llm-proxy` | `client-token`: the bearer token the proxy's clients, the task pods, send                             |
| `agentic-analysis-nrp`       | `api-key`: the API key of [NRP's managed LLMs](https://nrp.ai/documentation/userdocs/ai/llm-managed/) |

Each is in this directory, encrypted with SOPS.

## LLM proxy

`llm_proxy.py` is an OpenAI-compatible proxy for
[NRP's managed LLMs](https://nrp.ai/documentation/userdocs/ai/llm-managed/),
and the only holder of the NRP key. It serves `/v1/chat/completions` behind the
`agentic-analysis-llm-proxy` Service to clients that send `client-token` as a
bearer token. Each request names a role of `models.toml` as its model; the
proxy sends it to that role's first healthy backend that has room for it, or to
the one its session (`x-af-session`) started on, and keeps every request within
NRP's fair use:

- it admits a request when its model has a free slot, room in the tokens its
  requests in flight may hold, and room in its output per minute, the waiting
  ones in order of their role's class;
- it caps a reply's `max_tokens` at what the model's recent slow speed decodes
  before NRP ends a stream; it halves a model's slots when the model slows down
  or is rate-limited, and cuts them to one when it keeps failing;
- it answers at once and keeps the stream alive while a request waits, then
  passes the reply on only once it is whole: a reply cut off is asked for again
  with less output, and tool arguments that do not parse are asked for once
  more without streaming;
- it refuses bodies, image counts and prompt sizes the models would not take,
  as a context overflow, which OpenCode answers by compacting the session.

`/v1/models` lists the roles, `/config` their contexts and image counts for the
runner, `/status` each model's slots and load, `/healthz` that it runs. Each
request it forwards leaves one JSON line in its log. The proxy exits once the kubelet's
copy of either file differs from the one it started with, and starts again with
it.
