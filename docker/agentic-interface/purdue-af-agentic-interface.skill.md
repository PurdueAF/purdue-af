---
name: purdue-af-agentic-interface
description: Manage a Purdue Analysis Facility session — start/stop/restart the JupyterHub pod, connect via SSH, and inspect Dask clusters, storage, and logs. Use whenever the user mentions Purdue AF, their analysis-facility session, AF Dask clusters, or AF logs/storage.
---

# Purdue Analysis Facility — Agentic Interface

> **One-time setup**: the MCP server is registered in the project's `.mcp.json`,
> which reads your JupyterHub API token from `~/.config/purdue-af/token`. Get a
> token at https://cms.geddes.rcac.purdue.edu/hub/token, then:
> ```bash
> mkdir -p ~/.config/purdue-af && chmod 700 ~/.config/purdue-af
> printf '%s' '<your-api-token>' > ~/.config/purdue-af/token
> chmod 600 ~/.config/purdue-af/token
> ```
> Username and active pod are resolved automatically from the token.

This MCP server is **self-describing**: every tool result names the next step, so
the reliable way to drive a workflow is to call a tool and follow its returned
hints. The server also exposes invocable **prompts** (in Claude Code they appear
as `/mcp__purdue-af-agentic-interface__<name>`) that spell out the same playbooks:

| Prompt | What it does |
|---|---|
| `launch_session`  | start a session and wait until it is ready |
| `connect_session` | set up SSH and connect to a running session |
| `restart_session` | restart, preserving (or changing) profile/options |
| `stop_session`    | stop the session (storage is preserved) |
| `recover_ssh`     | fix a broken SSH connection after a restart |

**Always ask the user before connecting via SSH.**

---

## Connecting (the one flow worth spelling out)

After a session is running:

1. `prepare_ssh_connection` — returns one ready-to-run Bash command with your
   username and host already filled in. Run it locally in a single call.
2. If it prints `ALREADY_CONNECTED`, you're done. Otherwise it prints an SSH
   public key — pass that to `connect_to_session(public_key=...)`.
3. Verify: `ssh PurdueAF "hostname"`.

Thereafter, run pod commands with `ssh PurdueAF "<cmd>"`.

To auto-approve these in this project, add to `.claude/settings.local.json`:

```json
"permissions": {
  "allow": [
    "Bash(ssh PurdueAF *)",
    "Bash(ssh-keygen -t ed25519 -f ~/.ssh/af_key *)"
  ]
}
```

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

**`list_af_profiles`** — available profiles with exact option keys and choice values.
Call before `start_af_session` when non-default options are needed.

**`start_af_session`**
```json
{"name": "start_af_session", "arguments": {
  "profile_name": "<slug from list_af_profiles>",
  "user_options": {"<option-key>": "<choice-value>"}
}}
```
Omit both arguments for defaults (stable profile, JupyterLab, no GPU). Examples:
```json
{"0-cpu": "3", "3-interface": "2"}          // stable: 32 CPUs, VS Code
{"profile_name": "latest-pre-release-version", "user_options": {"interface": "1"}}
```

**`wait_for_session`** — poll until the pod is ready. Use this right after
`start_af_session` instead of looping `get_session_status`.

**`restart_af_session`** — stop + start, preserving options by default. Pass
`profile_name` / `user_options` to change configuration on restart.

**`stop_af_session`** — stops the pod. Storage (home, /work) is always preserved.

### Connecting

**`prepare_ssh_connection`** — returns the exact local SSH-setup command
(username/host baked in). First step of connecting; see above.

**`connect_to_session(public_key)`** — injects the public key into the pod's
`~/.ssh/authorized_keys` (idempotent). The pod's web interface is at
`https://cms.geddes.rcac.purdue.edu/user/<username>/`.

### Storage

**`query_storage_usage`** — home and work directory quotas (Prometheus, ≤ 5 min stale).
Requires a running pod.

### Dask clusters
*(Results always scoped to the calling user.)*

**`list_dask_clusters`** — all clusters across every gateway.
**`get_dask_cluster_info(cluster_name, gateway="k8s")`** — per-worker state and options.
**`scale_dask_cluster(cluster_name, n_workers, gateway="k8s")`**
**`stop_dask_cluster(cluster_name, gateway="k8s")`** — irreversible.

`gateway` options: `"k8s"` · `"slurm-hammer"` · `"slurm-gautschi"` · `"slurm"`

### Logs

**`query_notebook_logs`** — JupyterLab / VS Code server logs. Requires a running pod.
**`query_dask_logs`** — Dask worker and scheduler logs (notebook pod excluded).

Both accept `start` (`"1h"`, `"30m"`, or ISO-8601), `limit` (default 500), and
`filter` (LogQL pipe expression, e.g. `"|= \"ERROR\""`).

---

## Authentication errors

| Symptom | Cause |
|---|---|
| `{"error":"Missing Bearer token"}` | `JUPYTERHUB_TOKEN` not set |
| `{"error":"Invalid JupyterHub token"}` | Token expired — refresh at `/hub/token` |
| `"no running server found"` in result | Pod not running — use `start_af_session` |
| HTTP 404 on the service URL | Service not deployed or not registered with JupyterHub |

---

## Service endpoint (for manual testing)

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
