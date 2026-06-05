# Purdue Analysis Facility — Agentic Interface Skill

> **Installation**: copy this file into your project as
> `.claude/skills/purdue-af-agentic-interface.md`, or paste it into `CLAUDE.md`.
>
> **Required environment variables** (set once in your shell profile):
> ```bash
> export JUPYTERHUB_TOKEN=<your-api-token>   # from https://cms.geddes.rcac.purdue.edu/hub/token
> ```
> No `JUPYTERHUB_USER` needed — the service resolves your identity from the token.

---

## When to use these tools

When the user asks about logs, errors, output, or storage usage related to their
running Purdue AF pod — use the tools below.  The service is always available at
`https://cms.geddes.rcac.purdue.edu/services/agentic-interface/mcp` regardless of
whether the user has an active pod.

Base URL for all calls:
```
https://cms.geddes.rcac.purdue.edu/services/agentic-interface/mcp
```

---

## Tool: query_notebook_logs

Query logs from the **JupyterLab / VS Code server** container of the user's pod.

**Parameters** (all optional):

| Name | Type | Default | Description |
|------|------|---------|-------------|
| `start` | string | `"1h"` | Duration ago (`"1h"`, `"30m"`) or ISO-8601 timestamp |
| `end` | string | now | ISO-8601 end timestamp |
| `limit` | int | 500 | Maximum log lines (cap: 5000) |
| `filter` | string | — | LogQL pipe expression, e.g. `\|= "ERROR"` |

**How to call it:**
```bash
curl -s \
  -H "Authorization: Bearer ${JUPYTERHUB_TOKEN}" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "MCP-Protocol-Version: 2025-03-26" \
  -X POST \
  "https://cms.geddes.rcac.purdue.edu/services/agentic-interface/mcp" \
  -d '{
    "jsonrpc": "2.0", "id": 1,
    "method": "tools/call",
    "params": {
      "name": "query_notebook_logs",
      "arguments": { "start": "1h", "limit": 500 }
    }
  }' | grep '^data:' | sed 's/^data: //' \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['content'][0]['text'])"
```

---

## Tool: query_dask_logs

Query logs from the user's **Dask worker and scheduler pods**.  Same parameters
as `query_notebook_logs`.

Change `"name"` to `"query_dask_logs"` in the curl command above.

---

## Tool: query_storage_usage

Report disk quota and usage for the user's **home** and **work** directories.
Data comes from Prometheus (scraped every 5 min from af-pod-monitor). No parameters.

```bash
curl -s \
  -H "Authorization: Bearer ${JUPYTERHUB_TOKEN}" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "MCP-Protocol-Version: 2025-03-26" \
  -X POST \
  "https://cms.geddes.rcac.purdue.edu/services/agentic-interface/mcp" \
  -d '{
    "jsonrpc": "2.0", "id": 1,
    "method": "tools/call",
    "params": { "name": "query_storage_usage", "arguments": {} }
  }' | grep '^data:' | sed 's/^data: //' \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['content'][0]['text'])"
```

---

## Tools: Dask cluster management

`list_dask_clusters` — list running clusters (name, status, workers, scheduler address)
`get_dask_cluster_info(cluster_name)` — full details including per-worker state
`scale_dask_cluster(cluster_name, n_workers)` — set worker count
`stop_dask_cluster(cluster_name)` — shut down a cluster and release resources

All Dask tools use `"name": "list_dask_clusters"` etc. with `"arguments": {"cluster_name": "..."}`.

---

## Tools: AF session lifecycle

`get_session_status` — whether the pod is running, which profile, uptime, URL
`start_af_session(profile?, interface?, gpu?)` — start the pod; options:
  - `profile`: `"stable"` (default) or `"pre-release"` (includes AI sidecar)
  - `interface`: `"lab"` (JupyterLab, default) or `"vscode"`
  - `gpu`: `"0"` (none), `"1_mig"` (5 GB slice), `"1_a100"` (full 40 GB)
`stop_af_session` — stop the running pod (storage is preserved)

---

## Authentication errors

| Symptom | Likely cause |
|---------|-------------|
| `{"error":"Missing Bearer token"}` | `JUPYTERHUB_TOKEN` is unset |
| `{"error":"Invalid JupyterHub token"}` | Token expired — refresh at `/hub/token` |
| `"no running server found"` in result | User has no active pod |
| HTTP 404 on the URL | Service is not deployed or not yet registered with JupyterHub |
