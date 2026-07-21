# Agentic interface (MCP server)

Purdue AF provides a remote [MCP](https://modelcontextprotocol.io) (Model Context
Protocol) server that lets you manage your Analysis Facility session from **any
MCP-capable AI agent** — Claude Code, Codex, Cursor, and others. Once connected,
you can control the AF in natural language: start and stop your session, and
inspect your Dask clusters, storage usage, and logs.

## Connecting your agent

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

Your username and session are resolved automatically from the token, so you can
simply ask in plain language, for example:

* "Start my AF session" (optionally: "…with 32 CPUs and the VS Code interface")
* "How much home and work storage am I using?"
* "List my Dask clusters" / "scale `<name>` to 10 workers"
* "Create a Dask cluster" — the agent walks you through multiple-choice
  questions (backend, worker environment, worker size, and worker count) before
  creating it
* "Show the last 30 minutes of error logs from my notebook"

The available tools cover:

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

| Symptom | Cause / solution |
| --- | --- |
| `401` / "Invalid JupyterHub token" | The token expired or is wrong — get a new one at [/hub/token](https://cms.geddes.rcac.purdue.edu/hub/token). |
| "No active session" | No session is running — ask the agent to start one first. |
| HTTP 404 on the service URL | Check the URL — it must end with `/services/agentic-interface/mcp`. |

!!! note "See also"

    * [Access via VSCode-based IDEs](guide-ide-connection.md)
    * [Dask Gateway at Purdue AF](guide-dask-gateway.md)
