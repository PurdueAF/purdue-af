# Purdue AF Agentic Interface

A remote [MCP](https://modelcontextprotocol.io) server for the Purdue Analysis Facility.
Connect any MCP-capable agent (Claude Code, Codex, Cursor, …) and manage your AF session
in natural language — start/stop it, and inspect Dask clusters, storage, and logs.

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

For best results also install the companion skill,
[`.claude/skills/purdue-af-agentic-interface/SKILL.md`](../../.claude/skills/purdue-af-agentic-interface/SKILL.md)
— a portable Markdown playbook of the AF workflows. In Claude Code, save it as
`~/.claude/skills/purdue-af-agentic-interface/SKILL.md`; for other agents, paste it into
your persistent instructions (`AGENTS.md`, rules file, …).

## Use

Ask in plain language, for example:

- "Start my AF session" (optionally: "…with 32 CPUs and the VS Code interface")
- "How much home and work storage am I using?"
- "List my Dask clusters" / "scale `<name>` to 10 workers"
- "Show the last 30 minutes of error logs from my notebook"

## Troubleshooting

- **401 / invalid token** — your token expired or is wrong; get a new one at `/hub/token`.
- **"No active session"** — start a session first.
- Treat the token like a password — don't share or commit it.

## Calling the endpoint by hand

The deployed service runs with **stateful** streamable-HTTP sessions
(`MCP_STATELESS_HTTP=false`) so tools can use elicitation. A one-shot
`tools/call` therefore needs a prior `initialize` + `Mcp-Session-Id` handshake —
use a real MCP client for interactive testing, or set `MCP_STATELESS_HTTP=true`
on the deployment for stateless one-shot calls.

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

Tool names, arguments and limits are not documented here: the server is
self-describing, so `tools/list` (or any MCP client's tool inspector) is the
source of truth. Agent-facing guidance lives in
[the skill](../../.claude/skills/purdue-af-agentic-interface/SKILL.md).

From inside an AF session the service is reachable in-cluster and the session's
own token authenticates it — `config-agents.sh` registers it automatically:

```
http://agentic-interface.${NAMESPACE}.svc.cluster.local:8888/services/agentic-interface/mcp
```
