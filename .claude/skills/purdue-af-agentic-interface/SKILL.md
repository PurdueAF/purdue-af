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

Deployment details, and how to call the endpoint by hand, are in
[apps/agentic-interface/README.md](../../../apps/agentic-interface/README.md).
