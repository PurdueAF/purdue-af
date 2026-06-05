# Purdue Analysis Facility — Agentic Interface

> **Setup**: set once in your shell profile:
> ```bash
> export JUPYTERHUB_TOKEN=<your-api-token>
> # Obtain at: https://cms.geddes.rcac.purdue.edu/hub/token
> ```
> Your username and active pod are resolved automatically from the token.

---

## Workflows — read this first

Follow these sequences exactly.  The tool reference is in the sections below.

### Starting a session

```
1. get_session_status
     → if already "running": report status and URL, then stop — do not re-start

2. start_af_session (with any desired profile/options)

3. Poll until ready:
     repeat every 15 s: get_session_status
     stop when status = "running"  (typically 30–60 s)

4. Report: "Session is running at <url>. Would you like to connect to it via SSH?"
     → wait for user's answer before proceeding
```

### Connecting to a running session
*(Only run this workflow when the user explicitly asks to connect.)*

```
1. get_session_status  →  confirm status = "running"; note the username.

2. Bash (one call — approval 1):
   Replace USERNAME with the value from step 1, then run the full script.
   It tries to connect first; only runs setup if that fails.

     USERNAME="<username>"
     if ssh -o BatchMode=yes -o ConnectTimeout=5 PurdueAF "hostname" 2>/dev/null; then
       echo "ALREADY_CONNECTED"
     else
       [ -f ~/.ssh/af_key ] \
         || ssh-keygen -t ed25519 -f ~/.ssh/af_key -N "" -C "purdue-af-agentic" -q
       grep -q "Host PurdueAF" ~/.ssh/config 2>/dev/null \
         || cat >> ~/.ssh/config << EOF
Host PurdueAF
    HostName cms.geddes.rcac.purdue.edu
    Port 22
    User ${USERNAME}
    IdentityFile ~/.ssh/af_key
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
    ControlMaster auto
    ControlPath ~/.ssh/control-af-%r@%h:%p
    ControlPersist 60m
EOF
       cat ~/.ssh/af_key.pub
     fi

   → If output is "ALREADY_CONNECTED": done, skip steps 3 and 4.
   → Otherwise the output is the public key — continue to step 3.

3. call connect_to_session(public_key=<output from step 2>)

4. Bash (one call — approval 2):
     ssh PurdueAF "hostname"
```

### Stopping a session

```
1. Bash: ssh -O exit PurdueAF 2>/dev/null; true
     (closes the ControlMaster gracefully; the "; true" suppresses errors
      if no SSH connection is currently open)

2. stop_af_session
```

### Restarting a session

```
1. Bash: ssh -O exit PurdueAF 2>/dev/null; true

2. restart_af_session (with any desired profile/options)

3. Poll until ready:
     repeat every 15 s: get_session_status
     stop when status = "running"

4. Report: "Session has restarted. Would you like to reconnect via SSH?"
     → if yes: follow the "Connecting to a running session" workflow above
```

### Reducing future approval prompts (optional)

To make `ssh PurdueAF` and key-generation commands auto-approved in this project,
add them to `.claude/settings.local.json`:

```json
"permissions": {
  "allow": [
    "Bash(ssh PurdueAF *)",
    "Bash(ssh-keygen -t ed25519 -f ~/.ssh/af_key *)"
  ]
}
```

### Getting a link to open the session in a browser

```
call get_session_status
```

The response always includes both interface links — JupyterLab and VS Code —
with the one that was active at spawn time marked `← active`.  Present them
as clickable links for the user to open in their browser.

Works at any time: if no session is running the links are still shown;
they redirect to the JupyterHub spawn form.

### Recovering a broken SSH connection

```
Bash: rm -f ~/.ssh/control-af-* && ssh PurdueAF "hostname"
```
Use this when SSH commands return a socket error after a pod restart or
network interruption.  It clears the stale ControlMaster socket and opens
a fresh connection.

---

## Tool reference

### Session lifecycle
*(All tools work even when no pod is running.)*

**`get_session_status`** — current pod state, profile selected, uptime, URL.

**`list_af_profiles`** — available profiles with exact option keys and choice values.
Call this before `start_af_session` when non-default options are needed.

**`start_af_session`**
```json
{"name": "start_af_session", "arguments": {
  "profile_name": "<slug from list_af_profiles>",
  "user_options": {"<option-key>": "<choice-value>"}
}}
```
Omit both arguments for defaults (stable profile, JupyterLab, no GPU).

Examples:
```json
{"0-cpu": "3", "3-interface": "2"}          // stable: 32 CPUs, VS Code
{"profile_name": "latest-pre-release-version", "user_options": {"interface": "1"}}
```

**`restart_af_session`** — stop + start, preserving options by default.
Pass `profile_name` / `user_options` to change configuration on restart.

**`stop_af_session`** — stops the pod. Storage (home, /work) is always preserved.

---

### Connecting to a session

**`connect_to_session(public_key)`** — injects the SSH public key into the pod's
`~/.ssh/authorized_keys`, checking first whether it is already present (idempotent).
Returns the SSH config block and connection instructions.

The pod's web interface is at `https://cms.geddes.rcac.purdue.edu/user/<username>/`.

---

### Storage

**`query_storage_usage`** — home and work directory quotas (Prometheus, ≤ 5 min stale).
Requires a running pod.

---

### Dask clusters
*(Results always scoped to the calling user.)*

**`list_dask_clusters`** — all clusters across every gateway (k8s, slurm-hammer, slurm-gautschi, slurm).

**`get_dask_cluster_info(cluster_name, gateway="k8s")`** — per-worker state and options.

**`scale_dask_cluster(cluster_name, n_workers, gateway="k8s")`**

**`stop_dask_cluster(cluster_name, gateway="k8s")`** — irreversible.

`gateway` options: `"k8s"` · `"slurm-hammer"` · `"slurm-gautschi"` · `"slurm"`

---

### Logs

**`query_notebook_logs`** — JupyterLab / VS Code server logs. Requires a running pod.

**`query_dask_logs`** — Dask worker and scheduler logs (notebook pod excluded).

Both accept `start` (duration `"1h"`, `"30m"` or ISO-8601), `limit` (default 500),
and `filter` (LogQL pipe expression, e.g. `"|= \"ERROR\""`).

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
