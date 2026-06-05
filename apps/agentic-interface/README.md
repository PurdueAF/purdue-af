# Purdue AF Agentic Interface

A remote [MCP](https://modelcontextprotocol.io) server for the Purdue Analysis Facility.
Connect any MCP-capable agent (Claude Code, Codex, Cursor, …) and manage your AF session
in natural language — start/stop it, connect over SSH, and inspect Dask clusters,
storage, and logs.

## Connect

| | |
|---|---|
| **URL** | `https://cms.geddes.rcac.purdue.edu/services/agentic-interface/mcp` |
| **Transport** | HTTP (streamable) |
| **Auth** | header `Authorization: Bearer <token>` |

Get your token at <https://cms.geddes.rcac.purdue.edu/hub/token>, then add the server in
your agent's MCP settings. Most agents accept this configuration:

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

Where this goes depends on your agent — a config file, a settings panel, or a CLI
(`claude mcp add`, `codex mcp add`, …) — but the URL and header are always the same. If
your agent expands environment variables in its config, use `Bearer ${JUPYTERHUB_TOKEN}`
instead of pasting the token.

## Use

Ask in plain language, for example:

- "Start my AF session" (optionally: "…with 32 CPUs and the VS Code interface")
- "Connect me to my session over SSH"
- "How much home and work storage am I using?"
- "List my Dask clusters" / "scale `<name>` to 10 workers"
- "Show the last 30 minutes of error logs from my notebook"

Connecting over SSH runs a couple of commands on your own machine; your agent will ask
before running them.

## Troubleshooting

- **401 / invalid token** — your token expired or is wrong; get a new one at `/hub/token`.
- **"no running server found"** — start a session first.
- Treat the token like a password — don't share or commit it.
