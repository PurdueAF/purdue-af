# Agentic interface (MCP server)

Purdue AF provides a remote [MCP](https://modelcontextprotocol.io) (Model Context
Protocol) server that lets you manage your Analysis Facility session from **any
MCP-capable AI agent** — Claude Code, Codex, Cursor, and others. You can control
the AF in natural language: start and stop your session, check whether the
facility is healthy, and inspect your Dask clusters, storage usage, and logs.

Agents inside an AF session are ready to use; agents on your own machine need a
token and a one-time setup.

## Inside an AF session

Nothing to set up. `claude` and `codex` are on `PATH` in any terminal, their
extensions are installed in the VS Code interface, and both are already
connected to the MCP server as you — no token, re-registered on every session
start. The AF skill and a short platform context (storage volumes, scale-out
limits, GPU options) are installed too.

Run `claude` or `codex` and ask for what you want. You still sign in to the
agent with your own Anthropic or OpenAI account; the AF ships no model
credentials.

## Connecting from your own machine

|               |                                                                    |
| ------------- | ------------------------------------------------------------------ |
| **URL**       | `https://cms.geddes.rcac.purdue.edu/services/agentic-interface/mcp` |
| **Transport** | HTTP (streamable)                                                  |
| **Auth**      | header `Authorization: Bearer <token>`                             |

1. Obtain a JupyterHub API token at
   [https://cms.geddes.rcac.purdue.edu/hub/token](https://cms.geddes.rcac.purdue.edu/hub/token).
2. Add the server in your agent's MCP settings. Most agents accept this
   configuration:

    ```json
    {
      "mcpServers": {
        "purdue-af": {
          "type": "http",
          "url": "https://cms.geddes.rcac.purdue.edu/services/agentic-interface/mcp",
          "headers": { "Authorization": "Bearer YOUR_TOKEN" }
        }
      }
    }
    ```

    Where this goes depends on your agent — a config file, a settings panel, or a
    CLI command (`claude mcp add`, `codex mcp add`, …) — but the URL and header are
    always the same. If your agent expands environment variables in its config,
    use `Bearer ${JUPYTERHUB_TOKEN}` instead of pasting the token.

!!! warning "Treat the token like a password"

    The token gives full control over your AF session — do not share it or commit
    it to a Git repository.

??? example "Example: connecting Claude Code"

    Store the token in a file (instead of pasting it into a config), then
    register the server at user scope so it is available in every project:

    ```bash
    mkdir -p ~/.config/purdue-af && chmod 700 ~/.config/purdue-af
    printf '%s' '<your-api-token>' > ~/.config/purdue-af/token
    chmod 600 ~/.config/purdue-af/token

    claude mcp add --scope user --transport http purdue-af-agentic-interface \
      https://cms.geddes.rcac.purdue.edu/services/agentic-interface/mcp \
      --header "Authorization: Bearer $(cat ~/.config/purdue-af/token)"
    ```

## Installing the skill (recommended)

!!! note

    Not needed inside an AF session — the skill is already installed there.
    This is for agents running on your own machine.

The MCP server is self-describing, but agents work noticeably better with the
accompanying **skill** — a Markdown playbook that teaches the agent the AF
workflows (how to launch a session, which tools to call in what order). It lives
in the Purdue AF repository:
[`.claude/skills/purdue-af-agentic-interface/SKILL.md`](https://github.com/PurdueAF/purdue-af/blob/main/.claude/skills/purdue-af-agentic-interface/SKILL.md)

For **Claude Code**, install it with:

```bash
mkdir -p ~/.claude/skills/purdue-af-agentic-interface
curl -fsSL -o ~/.claude/skills/purdue-af-agentic-interface/SKILL.md \
  https://raw.githubusercontent.com/PurdueAF/purdue-af/main/.claude/skills/purdue-af-agentic-interface/SKILL.md
```

The skill then activates automatically whenever you mention your Purdue AF
session, Dask clusters, or AF logs/storage.

For **other agents**, copy the same file's contents into whatever your agent
uses for persistent instructions (e.g. `AGENTS.md`, Cursor rules, a custom
system prompt) — it is plain Markdown with no Claude-specific content beyond
the front-matter header.

## What you can do

Your username and session are resolved automatically, so you can simply ask in
plain language, for example:

* "Start my AF session" (optionally: "…with 32 CPUs and the VS Code interface")
* "How much home and work storage am I using?"
* "List my Dask clusters" / "scale `<name>` to 10 workers"
* "Create a Dask cluster" — the agent walks you through multiple-choice
  questions (backend, worker environment, worker size, and worker count) before
  creating it
* "Show the last 30 minutes of error logs from my notebook"
* "Is the AF healthy?" — what is affecting the facility, if anything, and for
  how long

The available tools cover:

* **Facility health** — a summary of anything currently affecting the facility:
  access, storage, scale-out, and the software environment.
* **Session lifecycle** — check status, list available resource profiles,
  start / stop / restart the session, and wait until it is ready. When starting,
  the agent asks you (as multiple-choice questions) which profile and resource
  options — interface, CPU, memory — to use, unless you ask for the defaults.
* **Storage** — home and work directory quota usage.
* **Dask clusters** — list, create (Kubernetes or Slurm/Hammer), inspect, check worker
  counts and CPU/memory usage, scale, and shut down your
  [Dask Gateway](guide-dask-gateway.md) clusters on either gateway
  (`k8s` or `slurm`).
* **Logs** — query your JupyterLab / VS Code server logs and Dask worker and
  scheduler logs, with time ranges and filters.

The server also exposes invocable **workflow prompts** (`launch_session`,
`restart_session`, `stop_session`, `create_cluster`) that walk the agent through
each multi-step workflow. In Claude Code they appear as
`/mcp__purdue-af-agentic-interface__<name>` slash commands.

## Troubleshooting

Every failure is reported where you can see it: in the tool result the agent
relays to you, or — when the connection itself is refused — in the server's
response, which carries an `error` and a `hint` saying what to do.

| Symptom | Cause / solution |
| --- | --- |
| `Missing Bearer token`, `Empty Bearer token`, `Unexpanded token placeholder` | The token never reached the server: the token file or environment variable in your agent's MCP configuration is missing, empty, or was not expanded. Fix it and reconnect the server. |
| `Invalid JupyterHub token` | The token expired, is mistyped, or was revoked — get a new one at [/hub/token](https://cms.geddes.rcac.purdue.edu/hub/token). Inside an AF session the token rotates on every restart, so restart the agent. |
| `JupyterHub API unavailable` (HTTP 503) | The hub cannot validate tokens right now — a facility problem, not a token problem. Retry in a minute. |
| "not permitted … (HTTP 403)" | A session's own token can read the session but not start, stop, or restart it. From your own machine use a token from [/hub/token](https://cms.geddes.rcac.purdue.edu/hub/token). |
| "Cannot read session state" | The agent's token is not allowed to list your sessions. Inside an AF session this means the image predates the fix — restart the session; from your own machine, mint a fresh token. |
| "unreachable" / "returned HTTP 5xx" / "could not read … metrics" | A facility backend (hub, Dask gateway, log store, monitoring) is down or restarting. Ask the agent whether the facility is healthy, and retry in a minute. |
| "No active session" | No session is running — ask the agent to start one first. |
| "failed unexpectedly" | A fault in the service itself — [contact support](support.md) with the tool name and the time. |
| Agent reports a facility problem | Check the [monitoring dashboard](https://cms.geddes.rcac.purdue.edu/grafana/d/purdue-af-alerts) and [contact support](support.md) if it persists. |
| HTTP 404 on the service URL | Check the URL — it must end with `/services/agentic-interface/mcp`. |

!!! note "See also"

    * [Access via VSCode-based IDEs](guide-ide-connection.md)
    * [Dask Gateway at Purdue AF](guide-dask-gateway.md)
