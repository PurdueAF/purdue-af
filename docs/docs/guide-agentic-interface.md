# Agentic interface (MCP server)

Purdue AF provides a remote [MCP](https://modelcontextprotocol.io) (Model Context
Protocol) server that lets you manage your Analysis Facility session from **any
MCP-capable AI agent** — Claude Code, Codex, Cursor, and others. You can control
the AF in natural language: start and stop your session, check whether the
facility is healthy, and inspect your Dask clusters, storage usage, and logs.

Agents inside an AF session are ready to use; agents on your own machine need a
token and a one-time setup.

## Inside an AF session

Nothing to set up. `claude`, `codex` and `opencode` are on `PATH` in any
terminal, the Claude Code and Codex extensions are installed in the VS Code
interface, and all three are already connected to the MCP server as you — no
token, re-registered on every session start.

A short **platform context** is installed too: the facility's rules and
recommendations (storage volumes and quotas, where environments may live,
scale-out limits, GPU options) written into the file each agent reads
automatically at startup. You do not need to invoke anything — an agent in a
session already knows, for example, that `pixi install` will be refused under
`/home` and that `/work` is invisible to Slurm workers.

| Agent      | Where the context is installed                | MCP server                             |
| ---------- | --------------------------------------------- | -------------------------------------- |
| Claude Code | `~/.claude/CLAUDE.md` (plus the AF skill)     | `claude mcp add`, user scope           |
| Codex      | `~/.codex/AGENTS.md`                          | `codex mcp add`                        |
| opencode   | `~/.config/opencode/AGENTS.md`                | `$OPENCODE_CONFIG` config layer        |

Only the block between the `PURDUE AF — managed` markers belongs to the
facility; anything you write in those files outside it is preserved, and the
block is refreshed on every session start.

Run `claude`, `codex` or `opencode` and ask for what you want. You still sign in
to the agent with your own Anthropic, OpenAI or other provider account; the AF
ships no model credentials.

??? info "Why Cursor is not in that table"

    Cursor has no user-scope instruction file. Its rules are project-scoped
    (`.cursor/rules/`, or an `AGENTS.md` in a project root), and cross-project
    rules live in the Cursor UI under **Customize → Rules** rather than on
    disk — so there is nothing the facility can install on your behalf.

    Cursor also runs on your own machine rather than inside a session, so use
    the setup below, and paste the contents of
    [`platform-context.md`](https://github.com/PurdueAF/purdue-af/blob/main/docker/purdue-af/agents/platform-context.md)
    into your User Rules if you want the same guardrails.

!!! note "opencode brings its own model provider"

    The facility wires up the MCP server and the platform context, but opencode
    is not tied to one provider — run `/connect` once with your own API key.
    Its facility configuration lives in a separate layer
    (`~/.config/opencode/purdue-af.json`, referenced by `$OPENCODE_CONFIG`) that
    is merged with, and overridden by, your own `opencode.json`.

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
uses for persistent instructions — `~/.codex/AGENTS.md` for Codex,
`~/.config/opencode/AGENTS.md` for opencode, **Customize → Rules** for Cursor.
It is plain Markdown with no Claude-specific content beyond the front-matter
header.

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

The server also exposes one invocable **workflow prompt**, `create_cluster`,
which walks the agent through the four cluster questions when its client
cannot ask them itself. In Claude Code it appears as
`/mcp__purdue-af-agentic-interface__create_cluster`.

## Troubleshooting

Failures are self-describing: a failing tool result (which the agent relays
to you) says what was attempted, why it failed, and what to do next. If the
connection itself is refused — the agent shows the server as failed or
needing authentication — the server's response carries an `error` and a
`hint` with the same information; ask the agent for it, or check your MCP
client's server status.

| Symptom | What to do |
| --- | --- |
| Agent reports a facility problem | Check the [monitoring dashboard](https://cms.geddes.rcac.purdue.edu/grafana/d/purdue-af-alerts) and [contact support](support.md) if it persists. |
| HTTP 404 on the service URL | Check the URL — it must end with `/services/agentic-interface/mcp`. |
| A message says the fault is in the service itself | [Contact support](support.md) with the tool name and the time. |

!!! note "See also"

    * [Access via VSCode-based IDEs](guide-ide-connection.md)
    * [Dask Gateway at Purdue AF](guide-dask-gateway.md)
