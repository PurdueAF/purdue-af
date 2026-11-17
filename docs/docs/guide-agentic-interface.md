# AI agents on your machine

Purdue AF runs a remote [MCP](https://modelcontextprotocol.io) (Model Context
Protocol) server through which an AI agent on your own machine — Claude Code,
Codex, Cursor and others — manages the facility for you: it starts and stops
your session, creates Dask clusters, submits Ray jobs, and reads your storage
use, your logs and this documentation.

Agents inside an AF session are connected already — see
[AI agents in a session](agents.md). The setup below is for an agent on your own machine.

## Connecting from your own machine

|               |                                                                    |
| ------------- | ------------------------------------------------------------------ |
| **URL**       | `https://cms.geddes.rcac.purdue.edu/services/agentic-interface/mcp` |
| **Transport** | HTTP (streamable)                                                  |
| **Auth**      | header `Authorization: Bearer <token>`                             |

Two steps, once per machine.

**1. Save a token.** Obtain a
[JupyterHub API token](guide-ssh-access.md#jupyterhub-api-token) and save it in
`~/.config/purdue-af/token`, as that page shows. Claude Code reads the file.
Every other agent reads the token from the `JUPYTERHUB_TOKEN` environment
variable, so add this line to your shell profile (`~/.zshrc` or `~/.bashrc`)
and open a new terminal:

```bash
export JUPYTERHUB_TOKEN="$(cat ~/.config/purdue-af/token)"
```

**2. Install for your agent.** Each set of commands adds the MCP server and the
[skill](#installing-the-skill-recommended).

=== "Claude Code"

    ```bash
    claude plugin marketplace add PurdueAF/purdue-af
    claude plugin install purdue-af@purdue-af
    ```

=== "Codex"

    ```bash
    codex plugin marketplace add PurdueAF/purdue-af
    codex plugin add purdue-af@purdue-af
    ```

=== "Copilot CLI"

    ```bash
    copilot plugin marketplace add PurdueAF/purdue-af
    copilot plugin install purdue-af@purdue-af
    ```

    VS Code lists a plugin installed with Copilot CLI on the **Plugins** page
    of its Agent Customizations editor.

=== "Gemini CLI"

    ```bash
    gemini mcp add --scope user --transport http \
      --header 'Authorization: Bearer ${JUPYTERHUB_TOKEN}' \
      purdue-af-agentic-interface \
      https://cms.geddes.rcac.purdue.edu/services/agentic-interface/mcp
    gemini skills install https://github.com/PurdueAF/purdue-af.git \
      --path .claude/skills/purdue-af-agentic-interface
    ```

=== "OpenCode"

    Add the server to `~/.config/opencode/opencode.json`:

    ```json
    {
      "$schema": "https://opencode.ai/config.json",
      "mcp": {
        "purdue-af-agentic-interface": {
          "type": "remote",
          "url": "https://cms.geddes.rcac.purdue.edu/services/agentic-interface/mcp",
          "headers": { "Authorization": "Bearer {env:JUPYTERHUB_TOKEN}" }
        }
      }
    }
    ```

    Then install the skill:

    ```bash
    npx skills add PurdueAF/purdue-af --global --agent opencode --yes
    ```

=== "Cursor"

    Add the server to `~/.cursor/mcp.json`:

    ```json
    {
      "mcpServers": {
        "purdue-af-agentic-interface": {
          "url": "https://cms.geddes.rcac.purdue.edu/services/agentic-interface/mcp",
          "headers": { "Authorization": "Bearer ${env:JUPYTERHUB_TOKEN}" }
        }
      }
    }
    ```

    Then install the skill:

    ```bash
    npx skills add PurdueAF/purdue-af --global --agent cursor --yes
    ```

=== "Other agents"

    Most agents accept this configuration — in a config file, a settings
    panel, or a CLI command:

    ```json
    {
      "mcpServers": {
        "purdue-af-agentic-interface": {
          "type": "http",
          "url": "https://cms.geddes.rcac.purdue.edu/services/agentic-interface/mcp",
          "headers": { "Authorization": "Bearer YOUR_TOKEN" }
        }
      }
    }
    ```

    If your agent expands environment variables in its config, refer to
    `JUPYTERHUB_TOKEN` there instead of pasting the token. Then install the
    skill, choosing your agent when asked:

    ```bash
    npx skills add PurdueAF/purdue-af --global
    ```

## Installing the skill (recommended)

The **skill** is a Markdown playbook that teaches an agent the AF workflows:
which tool to call when, and what to confirm with you first. The commands
[above](#connecting-from-your-own-machine) install it with the MCP server, and
it activates when you mention your AF session, Dask clusters, Ray jobs, logs or
storage.

The MCP server announces the current skill version, so the agent tells you when
your copy is out of date. Update it with your agent's own command:

| Agent        | Update                                                                                         |
| ------------ | ---------------------------------------------------------------------------------------------- |
| Claude Code  | `claude plugin marketplace update purdue-af`, then `claude plugin update purdue-af@purdue-af` |
| Codex        | `codex plugin marketplace upgrade purdue-af`                                                   |
| Copilot CLI  | `copilot plugin update purdue-af`                                                              |
| Gemini CLI   | the `gemini skills install` command again                                                      |
| Other agents | `npx skills update --global`                                                                   |

## What you can do

Your username and session are resolved automatically, so you ask in plain
language.

| Area | Ask, for example |
| --- | --- |
| Session | "Start my AF session with 32 CPUs and the VS Code interface" — the agent asks which profile and options you want unless you ask for the defaults. Also stop, restart and status. |
| Storage | "How much home and work storage am I using?" |
| Dask clusters | "Create a Dask cluster", "scale it to 10 workers", "how much memory did my workers use over the last day?", "shut it down" — see [Dask Gateway](guide-dask-gateway.md). |
| Ray on GPUs | "How many GPUs are free for 4 workers with T4s?", "Train `train.py` on 4 GPUs on NRP, with `/work/users/<username>/events` as input" — see [Ray jobs](guide-ray-train.md). |
| Logs | "Show the last 30 minutes of error logs from my notebook", or from your Dask workers. |
| Facility | "Is the AF healthy?", "How busy is it?", and any number the facility's monitoring holds. Other people's sessions, clusters and jobs are left out, so a sum over sessions or workers counts only yours. |
| Documentation | "How do I read a dataset through XCache?" — the agent reads the page of this site and links it. |

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

    * [Local IDE access](guide-ide-connection.md)
    * [Dask Gateway](guide-dask-gateway.md)
