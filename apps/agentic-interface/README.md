# Agentic interface

A remote [MCP](https://modelcontextprotocol.io) server through which any
MCP-capable agent manages a user's AF session: start and stop it, inspect Dask
clusters, storage and logs. Connecting, what it can do, troubleshooting:
[the user guide](../../docs/docs/guide-agentic-interface.md). The agent-facing
playbook is [the skill](../../.claude/skills/purdue-af-agentic-interface/SKILL.md).

| Path                                          | Holds                                                  |
| --------------------------------------------- | ------------------------------------------------------ |
| `apps/agentic-interface/`                     | Deployment, Service, RBAC, NetworkPolicy               |
| `docker/agentic-interface/`                   | Server source and Dockerfile                           |
| `.claude/skills/purdue-af-agentic-interface/` | The skill; an input of the image hash                  |
| `tests/agentic_interface/`                    | Unit tests                                             |

Versioning and rollout: [RELEASING.md](../../RELEASING.md).

## Calling the endpoint by hand

The service runs with **stateful** streamable-HTTP sessions
(`MCP_STATELESS_HTTP=false`) so tools can use elicitation. A one-shot
`tools/call` therefore needs a prior `initialize` + `Mcp-Session-Id` handshake:
use a real MCP client for interactive testing, or set `MCP_STATELESS_HTTP=true`
on the Deployment for stateless one-shot calls.

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

Tool names, arguments and limits are not documented anywhere: the server is
self-describing, and `tools/list` is the source of truth.

Inside a session the service is reached at its in-cluster address with the
session's own token; `config-agents.sh` in the session image registers it
([docker/purdue-af/README.md](../../docker/purdue-af/README.md)).
