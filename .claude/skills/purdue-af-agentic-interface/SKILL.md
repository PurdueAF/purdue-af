---
name: purdue-af-agentic-interface
description: Manage a Purdue Analysis Facility session — start/stop/restart the JupyterHub pod, run Ray jobs on GPUs, and inspect Dask clusters, storage, and logs. Use whenever the user mentions Purdue AF, their analysis-facility session, AF Dask clusters, Ray jobs or GPUs, or AF logs/storage.
---

# Purdue Analysis Facility — Agentic Interface

Skill version: `0.6`

> **Setup** — this skill drives the `purdue-af-agentic-interface` MCP server.
> Inside a Purdue AF session it is registered at startup with the session's own
> credentials. If its tools are missing there, the service is down: say so and
> stop. Never run the setup at the end of this file from inside a session; it
> would store a token in the home directory, which the session never does.

The server is **self-describing**: every tool carries its own arguments and
limits, and every result names the next step. Call a tool and follow what it
returns rather than planning the whole sequence up front. Everything below is
what the tool descriptions cannot tell you.

## Prefer these tools over the shell

Inside an AF session a terminal is right there, and it will give you the wrong
answer for anything the platform tracks centrally:

- **Storage and quotas** — `query_storage_usage`, never `du`/`df`. It reports
  usage against each per-user quota, which neither command knows about. Treat
  every quota as a hard limit.
- **Logs** — `query_notebook_logs` / `query_dask_logs`, never tailing files.
  Logs come from Loki and outlive the pod that produced them.
- **Session and cluster state** — the tools, never `kubectl` or `ps`. A session
  has no permission to see its own pod object.

## Before you call

- `list_af_profiles` before `start_af_session` when the user wants anything
  other than defaults — it carries the exact option keys, valid values, and
  live GPU availability.
- `wait_for_session` after starting a session, rather than polling
  `get_session_status` in a loop.
- `scale_dask_cluster` waits for the cluster's scheduler to come up on its own,
  so call it straight after `create_dask_cluster` rather than polling
  `get_dask_cluster_info` until the status turns RUNNING. Workers then register
  over the next half-minute or so, and `get_dask_worker_count` reads a
  Prometheus scrape — a low count right after a scale means the scrape has not
  caught up, not that the scale failed.

- `get_ray_capacity` before `submit_ray_job`, with the shape the job needs: it
  says how many workers are free at Purdue and on NRP, and so which site to
  name. `submit_ray_job` refuses an exact number of workers that is not free.
- `submit_ray_job` for work that runs by itself; `start_ray_cluster` only for
  interactive work from a notebook, whose `ray.init` call it returns. A cluster
  holds its GPUs while up, so `stop_ray_cluster` when that work is done.
- A Ray job on NRP sees none of `/work`, `/depot` or `/eos`: its code goes in
  `files`, its data through `inputs` and `output`, and the script reads
  `AF_INPUTS` and `AF_OUTPUT`. The Ray guide has a script for each use case;
  a script of `cuda` needs `entrypoint_gpus=1`, one of Ray Train, Tune or Data
  needs none.

## Actions that cost the user something

- **`restart_af_session` / `stop_af_session` kill the process you are running
  in** when you are inside the AF session itself. Say so and get agreement
  first; you will lose the conversation. Storage (home, `/work`) is preserved.
- **`stop_dask_cluster` is irreversible** — running work is lost.
- **`stop_ray_job` is irreversible** — the job keeps only what it saved.
  `stop_ray_cluster` with `force` loses the cluster's running work the same way.
- **Idle GPUs on NRP** stop a job, and NRP bans a namespace that keeps them
  idle: never leave a job there waiting on something other than its GPUs.
- Only **one active Dask cluster per user** is allowed, so creating one may
  require stopping the existing one.

## Reporting back

Work through the tool calls without narrating them, then report once at the end.
A run of "Checking the session…", "Still pending…", "Now scaling…" messages is
noise: the user cannot act on any of it, and it buries the actual result. This
is about the running commentary only — none of the substance below is optional.

- **Report the outcome once**, when the work is done: what now exists, its name
  and state, and the single next thing the user does. Session links and a Dask
  cluster's connect snippet belong here.
- **Speak up immediately, not at the end,** for anything that needs the user's
  agreement (see above), anything that blocks you, and anything you are about to
  do that they would not expect.
- **Flag what surprised you** — a call that failed and worked on retry, two
  tools that disagree, a limit you hit. One line each in the final report, even
  when the end state is fine. This interface gets fixed from those reports.
- **Summarise tool output, never paste it.** Tool results are written to steer
  you, not to be read by the user.
- **Skip process narration** — changes of approach, which tool you are reaching
  for next, notes you saved. Correct yourself out loud only when the correction
  changes what the user should do or believe.

Waiting is the tools' job, not the shell's: `wait_for_session` blocks until the
session is ready and returns as soon as it is. Never `sleep` in a shell to pace
a tool, and never poll one in a loop when a waiting variant exists.

## Presenting a session

`get_session_status` returns both interface links (JupyterLab and VS Code) with
the active one marked. Present them as clickable links — that is what the user
actually wants when they ask about their session. It works with no session
running too; the links land on the spawn form.

Deployment details, and how to call the endpoint by hand, are in
[apps/agentic-interface/README.md](../../../apps/agentic-interface/README.md).

## Setup outside a Purdue AF session

One-time, on a machine that is not an AF session:

1. Get a JupyterHub API token at https://cms.geddes.rcac.purdue.edu/hub/token
   and store it locally:
   ```bash
   mkdir -p ~/.config/purdue-af && chmod 700 ~/.config/purdue-af
   printf '%s' '<your-api-token>' > ~/.config/purdue-af/token
   chmod 600 ~/.config/purdue-af/token
   ```
2. Register the MCP server. Inside the PurdueAF/purdue-af repo the project
   `.mcp.json` does this automatically (it reads the token file above). In any
   other directory, register it at user scope — in Claude Code:
   ```bash
   claude mcp add --scope user --transport http purdue-af-agentic-interface \
     https://cms.geddes.rcac.purdue.edu/services/agentic-interface/mcp \
     --header "Authorization: Bearer $(cat ~/.config/purdue-af/token)"
   ```

Username and active pod are resolved automatically.
