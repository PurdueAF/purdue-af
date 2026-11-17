# Agent plugin

The `purdue-af` plugin gives an agent outside an AF session the
[agentic interface](../apps/agentic-interface/README.md): its MCP server entry
and [the skill](../.claude/skills/purdue-af-agentic-interface/SKILL.md).
Installing and updating it:
[the user guide](../docs/docs/guide-agentic-interface.md#connecting-from-your-own-machine).

| File                                 | What it is                                       |
| ------------------------------------ | ------------------------------------------------ |
| `.claude-plugin/marketplace.json`    | The marketplace: one plugin, rooted at `.claude` |
| `.claude/.claude-plugin/plugin.json` | The plugin's manifest                            |
| `.claude/.mcp.json`                  | The plugin's MCP server entry                    |
| `.claude/skills/`                    | The plugin's skills                              |

Claude Code, Codex and Copilot CLI read these files as they are. `.claude` is
the plugin root, so everything tracked under it is copied to whoever installs
the plugin.

The server entry carries the Hub token once per harness, and each harness
ignores the other keys:

| Key                    | Read by     | Token from                  |
| ---------------------- | ----------- | --------------------------- |
| `headersHelper`        | Claude Code | `~/.config/purdue-af/token` |
| `headers`              | Copilot CLI | `JUPYTERHUB_TOKEN`          |
| `bearer_token_env_var` | Codex       | `JUPYTERHUB_TOKEN`          |

No manifest states a version: each harness takes the commit it installed as the
plugin's version.

Check the manifests with `claude plugin validate .` from the repository root.
