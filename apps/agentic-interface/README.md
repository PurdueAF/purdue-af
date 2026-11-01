# Agentic interface

A remote [MCP](https://modelcontextprotocol.io) server through which any
MCP-capable agent manages a user's AF session: start and stop it, inspect Dask
clusters, storage and logs, and run Ray jobs. Connecting, what it can do, troubleshooting:
[the user guide](../../docs/docs/guide-agentic-interface.md). The agent-facing
playbook is [the skill](../../.claude/skills/purdue-af-agentic-interface/SKILL.md).

| Path                                          | Holds                                                        |
| --------------------------------------------- | ------------------------------------------------------------ |
| `apps/agentic-interface/`                     | Deployment, Service, RBAC, NetworkPolicy                     |
| `docker/agentic-interface/`                   | Server source and Dockerfile                                 |
| `.claude/skills/purdue-af-agentic-interface/` | The skill; bundled in the image, which announces its version |
| `tests/agentic_interface/`                    | Unit tests                                                   |

Versioning and rollout: [RELEASING.md](../../RELEASING.md).

## Calling the endpoint by hand

Call the endpoint from an MCP client: the service keeps stateful sessions, and a
plain `curl` POST of `tools/call` carries none, so it is refused.

Tool names, arguments and limits are not documented here: the server is
self-describing, and `tools/list` is the source of truth.

Inside a session the service is reached at its in-cluster address with the
session's own token; `config-agents.sh` in the session image registers it
([docker/purdue-af/README.md](../../docker/purdue-af/README.md)).
