# Purdue Analysis Facility — AI Sidecar Skill

> **Installation**: copy this file into your project as `.claude/skills/purdue-af-sidecar.md`,
> or paste it into your project's `CLAUDE.md` under a `## Skills` heading.
>
> **Required environment variables** (set once in your shell profile):
> ```bash
> export JUPYTERHUB_USER=<your-username>          # e.g. dkondra
> export JUPYTERHUB_TOKEN=<your-api-token>        # from https://cms.geddes.rcac.purdue.edu/hub/token
> ```

---

## When to use these tools

Whenever the user asks about logs, errors, or output from their running Purdue AF
pod — including the JupyterLab/VS Code server and any Dask workers — use the tools
below instead of `kubectl logs` (which may not be available locally).

---

## Tool: query_notebook_logs

Query logs from the **JupyterLab / VS Code server** container of the user's pod.
Use this for notebook kernel errors, extension issues, or server-side tracebacks.

**Parameters** (all optional):

| Name | Type | Default | Description |
|------|------|---------|-------------|
| `start` | string | `"1h"` | How far back: duration (`"1h"`, `"30m"`) or ISO-8601 timestamp |
| `end` | string | now | ISO-8601 end timestamp |
| `limit` | int | 500 | Maximum log lines to return (hard cap: 5000) |
| `filter` | string | — | LogQL pipe expression, e.g. `\|= "ERROR"` or `\|~ "timeout\|refused"` |

**How to call it:**
```bash
curl -s \
  -H "Authorization: Bearer ${JUPYTERHUB_TOKEN}" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "MCP-Protocol-Version: 2025-03-26" \
  -X POST \
  "https://cms.geddes.rcac.purdue.edu/user/${JUPYTERHUB_USER}/proxy/9191/mcp" \
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

Adjust `"start"`, `"limit"`, and `"filter"` in the `arguments` object as needed.

---

## Tool: query_dask_logs

Query logs from the user's **Dask worker and scheduler pods**.
Use this when diagnosing distributed computation failures or worker crashes.

Same parameters as `query_notebook_logs`.

**How to call it** — identical to above, change `"name"` to `"query_dask_logs"`:
```bash
curl -s \
  -H "Authorization: Bearer ${JUPYTERHUB_TOKEN}" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "MCP-Protocol-Version: 2025-03-26" \
  -X POST \
  "https://cms.geddes.rcac.purdue.edu/user/${JUPYTERHUB_USER}/proxy/9191/mcp" \
  -d '{
    "jsonrpc": "2.0", "id": 1,
    "method": "tools/call",
    "params": {
      "name": "query_dask_logs",
      "arguments": { "start": "1h", "limit": 500 }
    }
  }' | grep '^data:' | sed 's/^data: //' \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['content'][0]['text'])"
```

---

## Tool: query_storage_usage

Report disk quota and usage for the user's **home** and **work** directories.
Data comes from the af-pod-monitor sidecar (refreshed every 5 minutes) — no parameters needed.

**How to call it:**
```bash
curl -s \
  -H "Authorization: Bearer ${JUPYTERHUB_TOKEN}" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "MCP-Protocol-Version: 2025-03-26" \
  -X POST \
  "https://cms.geddes.rcac.purdue.edu/user/${JUPYTERHUB_USER}/proxy/9191/mcp" \
  -d '{
    "jsonrpc": "2.0", "id": 1,
    "method": "tools/call",
    "params": { "name": "query_storage_usage", "arguments": {} }
  }' | grep '^data:' | sed 's/^data: //' \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['content'][0]['text'])"
```

---

## Authentication errors

| Symptom | Likely cause |
|---------|-------------|
| `{"error":"Missing Bearer token"}` | `JUPYTERHUB_TOKEN` is unset |
| `{"error":"Invalid JupyterHub token"}` | Token expired — refresh at `/hub/token` |
| `{"error":"Token is for '…', not …"}` | Token belongs to a different user |
| HTTP 404 on the URL | Pod is not running, or was not started with the **pre-release** profile |
