---
name: purdue-af-agentic-interface
description: Manage a Purdue Analysis Facility session — start/stop/restart the JupyterHub pod, and inspect Dask clusters, storage, and logs. Use whenever the user mentions Purdue AF, their analysis-facility session, AF Dask clusters, or AF logs/storage.
---

# Purdue Analysis Facility — Agentic Interface

> **One-time setup** — this skill drives the `purdue-af-agentic-interface` MCP
> server. If its tools are not available, set it up:
>
> 1. Get a JupyterHub API token at https://cms.geddes.rcac.purdue.edu/hub/token
>    and store it locally:
>    ```bash
>    mkdir -p ~/.config/purdue-af && chmod 700 ~/.config/purdue-af
>    printf '%s' '<your-api-token>' > ~/.config/purdue-af/token
>    chmod 600 ~/.config/purdue-af/token
>    ```
> 2. Register the MCP server. Inside the PurdueAF/purdue-af repo the project
>    `.mcp.json` does this automatically (it reads the token file above). In any
>    other directory, register it at user scope — in Claude Code:
>    ```bash
>    claude mcp add --scope user --transport http purdue-af-agentic-interface \
>      https://cms.geddes.rcac.purdue.edu/services/agentic-interface/mcp \
>      --header "Authorization: Bearer $(cat ~/.config/purdue-af/token)"
>    ```
>
> Username and active pod are resolved automatically from the token.

This MCP server is **self-describing**: every tool result names the next step, so
the reliable way to drive a workflow is to call a tool and follow its returned
hints. The server also exposes invocable **prompts** (in Claude Code they appear
as `/mcp__purdue-af-agentic-interface__<name>`) that spell out the same playbooks:

| Prompt | What it does |
|---|---|
| `launch_session`  | start a session and wait until it is ready |
| `restart_session` | restart, preserving (or changing) profile/options |
| `stop_session`    | stop the session (storage is preserved) |

---

## Opening the session in a browser

Call `get_session_status` — the response always includes both interface links
(JupyterLab and VS Code), with the active one marked `← active`. Present them as
clickable links. Works even when no session is running (links redirect to the
spawn form).

---

## Tool reference

### Session lifecycle
*(All tools work even when no pod is running.)*

**`get_session_status`** — current pod state, profile selected, uptime, URL.

**`list_af_profiles`** — available profiles with exact option keys and choice values,
including live GPU availability per flavor (exhausted flavors are flagged "do not
select"). Call before `start_af_session` when non-default options are needed.

**`start_af_session`** — asks the user (via the client's multiple-choice UI /
MCP elicitation) for the profile, then one question per option (interface, CPU,
memory, …), unless supplied. Pass `use_defaults=true` to skip all questions and
launch the default profile. The GPU question shows how many of each flavor are
free right now (live from the same Prometheus source the Hub form uses) and hides
any flavor with none left. If the choices can't be collected interactively —
the client can't elicit, or the prompt was dismissed/cancelled — the tool does
NOT dead-end: it returns an instruction to ask the user (via `list_af_profiles`)
and re-call with `profile_name`/`user_options`, or `use_defaults=true`.
```json
{"name": "start_af_session", "arguments": {
  "profile_name": "<slug from list_af_profiles>",
  "user_options": {"<option-key>": "<choice-value>"}
}}
```
Supplying `profile_name`/`user_options` skips the matching questions;
`use_defaults` skips them all. Examples:
```json
{"use_defaults": true}                       // default profile, no questions
{"user_options": {"0-cpu": "3", "3-interface": "2"}}   // stable: 32 CPUs, VS Code
{"profile_name": "latest-pre-release-version", "user_options": {"interface": "1"}}
```

**`wait_for_session`** — poll until the pod is ready. Use this right after
`start_af_session` instead of looping `get_session_status`.

**`restart_af_session`** — stop + start, preserving options by default. Pass
`profile_name` / `user_options` to change configuration on restart.

**`stop_af_session`** — stops the pod. Storage (home, /work) is always preserved.

### Storage

**`query_storage_usage`** — home and work directory quotas (Prometheus, ≤ 5 min stale).
Works when a session is (or recently was) running so metrics exist.

### Dask clusters
*(Results always scoped to the calling user.)*

**`list_dask_clusters`** — all clusters across every gateway.
**`list_dask_cluster_options(gateway="k8s")`** — create-time fields/defaults/limits for a backend.
**`create_dask_cluster(gateway=None, env_source=None, …)`** — create a cluster.
**`get_dask_cluster_info(cluster_name, gateway="k8s")`** — status, options, dashboard.
**`get_dask_worker_count(cluster_name, gateway="k8s")`** — live worker count (by state).
**`get_dask_cluster_usage(cluster_name, gateway="k8s")`** — CPU/memory min/max/avg across Running workers.
**`scale_dask_cluster(cluster_name, n_workers, gateway="k8s")`**
**`stop_dask_cluster(cluster_name, gateway="k8s")`** — terminate/delete the cluster (irreversible).

`gateway` options: `"k8s"` (Geddes Kubernetes) · `"slurm"` (Hammer Slurm)

**Creating a cluster** — `create_dask_cluster` asks any omitted choice via the
client's multiple-choice UI (MCP elicitation), one question at a time. Clients
without elicitation get a text prompt listing the choices, or you can use the
`create_cluster` prompt to gather them in chat. The questions, in order:

1. `gateway`: `"k8s"` or `"slurm"`.
2. `env_source`: `"global"` (shared pixi env at `/work/pixi/global`, **k8s only**) ·
   `"pixi"` (your `pixi_project` [+ `pixi_env`]) · `"conda"` (your `conda_env`).
   Passing `pixi_project`/`conda_env` implies the matching source.
3. worker size: `default` (1 core / 4 GiB) or `custom` → then `worker_cores`
   (k8s ≤ 64, Slurm ≤ 16) and `worker_memory` (GiB, ≤ 64).
4. worker count to start with: `0`, `10`, `50`, or custom → `n_workers`.

Also optional: `env` (extra worker env vars). Passing `worker_cores`/`worker_memory`
skips question 3; passing `n_workers` skips question 4.

Create notes: one active cluster per user; Slurm workers cannot see `/work` (put
envs on `/depot`, and `"global"` is unavailable there); count 0 starts empty.

### Prompts (invocable playbooks)

`launch_session` · `restart_session` · `stop_session` · `create_cluster`

### Logs

**`query_notebook_logs`** — JupyterLab / VS Code server logs.
**`query_dask_logs`** — Dask worker and scheduler logs (notebook container excluded).

Both accept `start` (`"1h"`, `"30m"`, `"2d"`, or ISO-8601), `limit` (default 500),
and `filter` (LogQL pipe expression, e.g. `"|= \"ERROR\""`).

---

## Authentication errors

| Symptom | Cause |
|---|---|
| `{"error":"Missing Bearer token"}` | No Authorization header reached the server — check the MCP server config and `~/.config/purdue-af/token` |
| `{"error":"Invalid JupyterHub token"}` | Token expired — refresh at `/hub/token` |
| `"No active session"` in result | Pod not running — use `start_af_session` |
| HTTP 404 on the service URL | Service not deployed or not registered with JupyterHub |

---

## Service endpoint (for manual testing)

The deployed service runs with **stateful** streamable-HTTP sessions
(`MCP_STATELESS_HTTP=false`) so tools can use elicitation. That means a
one-shot `tools/call` curl (below) needs a prior `initialize` + `Mcp-Session-Id`
handshake — use a real MCP client for interactive testing, or set
`MCP_STATELESS_HTTP=true` on the deployment for stateless one-shot calls.

```bash
curl -s \
  -H "Authorization: Bearer ${JUPYTERHUB_TOKEN}" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "MCP-Protocol-Version: 2025-03-26" \
  -X POST \
  "https://cms.geddes.rcac.purdue.edu/services/agentic-interface/mcp" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"TOOL","arguments":ARGS}}' \
  | grep '^data:' | sed 's/^data: //' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['content'][0]['text'])"
```
