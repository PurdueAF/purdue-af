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

The server is **self-describing**: every tool carries its own arguments and
limits, and every result names the next step. Call a tool and follow what it
returns rather than planning the whole sequence up front. Everything below is
what the tool descriptions cannot tell you.

## Prefer these tools over the shell

Inside an AF session a terminal is right there, and it will give you the wrong
answer for anything the platform tracks centrally:

- **Storage and quotas** — `query_storage_usage`, never `du`/`df`. Quotas are
  per-user and enforced outside the filesystem; `du` reports neither.
- **Logs** — `query_notebook_logs` / `query_dask_logs`, never tailing files.
  Logs come from Loki and outlive the pod that produced them.
- **Session and cluster state** — the tools, never `kubectl` or `ps`. A session
  has no permission to see its own pod object.

## Before you call

- `list_af_profiles` before `start_af_session` when the user wants anything
  other than defaults — it carries the exact option keys, valid values, and
  live GPU availability.
- `list_dask_cluster_options` before `create_dask_cluster` — limits differ per
  gateway.
- `wait_for_session` after starting a session, rather than polling
  `get_session_status` in a loop.

## Actions that cost the user something

- **`restart_af_session` / `stop_af_session` kill the process you are running
  in** when you are inside the AF session itself. Say so and get agreement
  first; you will lose the conversation. Storage (home, `/work`) is preserved.
- **`stop_dask_cluster` is irreversible** — running work is lost.
- Only **one active Dask cluster per user** is allowed, so creating one may
  require stopping another.

## Presenting a session

`get_session_status` returns both interface links (JupyterLab and VS Code) with
the active one marked. Present them as clickable links — that is what the user
actually wants when they ask about their session. It works with no session
running too; the links land on the spawn form.

## When something fails

Every tool result that begins with `Error:` (or `Could not …`) is the final
answer for that call: it says what was attempted, why it failed as the backend
reported it, and what to do next. Relay it to the user in full. Do not retry
the same call unchanged, do not fall back to the shell, and do not invent a
cause the message does not give. A result describing an empty state ("No
running Dask clusters", "No logs found", "No storage metrics") is a real answer
from a backend that responded; a backend that could not be asked always says so.

### The server refuses the connection

If the client shows the server as failed or needing authentication and no tools
appear, the service answered HTTP 401 or 503 with a JSON body
`{"error": …, "hint": …}` — the hint is the diagnosis. Tell the user which case
applies:

| `error` | Meaning |
|---|---|
| `Missing Bearer token` | No Authorization header reached the server — check the MCP server config (outside an AF session, the token file or variable it reads) |
| `Empty Bearer token` | The header was sent, but the token file or environment variable it reads is empty or unset |
| `Unexpanded token placeholder` | The literal `${VAR}` / `YOUR_TOKEN` text from the config was sent — the variable was never set, or the client does not expand it |
| `Unsupported Authorization scheme` | The header was not `Bearer …` (JupyterHub's own `token …` scheme does not work here) |
| `Invalid JupyterHub token` | Expired, mistyped, or revoked. Inside an AF session, restart the agent so it picks up the new `JUPYTERHUB_API_TOKEN`; outside one, mint a new token at `/hub/token` |
| `JupyterHub API unavailable` (HTTP 503) | The hub could not validate any token — a facility problem, not a token problem; retry in a minute |
| HTTP 404 on the service URL | Wrong URL — it must end with `/services/agentic-interface/mcp` |

### A tool reports a failure

| Message contains | Meaning |
|---|---|
| `rejected this token (HTTP 401)` | The token was revoked after the connection was made (a session restart does this) — reconnect with the current token |
| `not permitted … (HTTP 403)` | A session's own token can read its session but not start, stop, or restart it — that needs a token minted at `/hub/token` |
| `not authorised on gateway` | No access to that Dask backend (Slurm needs a Hammer account) |
| `unreachable` / `returned HTTP 5xx` | A facility backend is down or restarting — `get_facility_health` says whether the facility is degraded; retry in a minute |
| `could not read … metrics` | Monitoring could not be asked — the thing being measured may be fine |
| `rejected the query (HTTP 400)` | The `filter` / time-range arguments are not valid LogQL — fix the arguments |
| `was called with invalid arguments` | Argument names or types do not match the tool description |
| `failed unexpectedly` | A fault in the service itself — report it to AF support with the tool name and time |
| `Session did not become ready` | The wait timed out; the message gives the last state seen and how many status checks failed |

Deployment details, and how to call the endpoint by hand, are in
[apps/agentic-interface/README.md](../../../apps/agentic-interface/README.md).
