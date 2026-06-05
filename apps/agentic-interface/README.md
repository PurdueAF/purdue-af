# Purdue AF Agentic Interface — user setup

Connect your own AI agent (e.g. [Claude Code](https://code.claude.com/docs)) to the
Purdue Analysis Facility and drive it in natural language — start/stop your session,
connect over SSH, inspect Dask clusters, check storage, and read logs.

The server already runs inside the facility. You only point your client at it and
authenticate with your personal JupyterHub token. Nothing needs to be installed on
the AF side.

> The other files in this directory (`deployment.yaml`, `service.yaml`, `rbac.yaml`,
> `kustomization.yaml`) are the Kubernetes manifests that deploy the server — you
> don't need them as a user.

---

## Prerequisites

- An account on the Purdue AF (you can sign in at <https://cms.geddes.rcac.purdue.edu>).
- [Claude Code](https://code.claude.com/docs) installed. *(Any MCP client works — see
  [Other MCP clients](#other-mcp-clients) — but the steps below are for Claude Code.)*

---

## 1. Get your API token

Open <https://cms.geddes.rcac.purdue.edu/hub/token>, request a new token, and copy it.
Your username and active session are resolved automatically from this token.

## 2. Save the token to a file

This keeps the secret out of your shell history and your client config, and lets you
rotate it by editing one file.

```bash
mkdir -p ~/.config/purdue-af && chmod 700 ~/.config/purdue-af
printf '%s' 'PASTE_YOUR_TOKEN_HERE' > ~/.config/purdue-af/token
chmod 600 ~/.config/purdue-af/token
```

*(Prefer the simplest possible setup? Skip this step and use
[Option B](#option-b--token-in-the-client-quick) below instead.)*

## 3. Register the MCP server

### Option A — token from the file (recommended)

Create a file named `.mcp.json` in the project directory where you'll run Claude Code:

```json
{
  "mcpServers": {
    "purdue-af-agentic-interface": {
      "type": "http",
      "url": "https://cms.geddes.rcac.purdue.edu/services/agentic-interface/mcp",
      "headersHelper": "printf '{\"Authorization\":\"Bearer %s\"}' \"$(tr -d '[:space:]' < \"$HOME/.config/purdue-af/token\")\""
    }
  }
}
```

`headersHelper` reads your token from the file each time the client connects, so the
token is never stored in this config (this file is safe to commit).

To make the server available in **every** project instead of just one, add the same
`"purdue-af-agentic-interface": { … }` block under the top-level `mcpServers` object in
`~/.claude.json`.

### Option B — token in the client (quick)

One command; the token is stored in your private `~/.claude.json`. Re-run it when the
token expires. (Skip step 2 if you use this.)

```bash
claude mcp add --transport http purdue-af-agentic-interface \
  https://cms.geddes.rcac.purdue.edu/services/agentic-interface/mcp \
  --header "Authorization: Bearer PASTE_YOUR_TOKEN_HERE" \
  --scope user
```

## 4. (Optional) Install the skill

The server is self-describing — every tool result tells the agent the next step, and it
ships ready-made workflow **prompts** — so it works fine with no extra setup. For the
smoothest experience in Claude Code, install the bundled skill:

```bash
mkdir -p ~/.claude/skills/purdue-af-agentic-interface
curl -fsSL https://raw.githubusercontent.com/PurdueAF/purdue-af/main/docker/agentic-interface/purdue-af-agentic-interface.skill.md \
  -o ~/.claude/skills/purdue-af-agentic-interface/SKILL.md
```

## 5. Start Claude Code and verify

1. Launch (or restart) Claude Code in the directory from step 3.
2. If you used a project `.mcp.json`, approve the project-server trust prompt on first
   launch (required because `headersHelper` runs a local command).
3. Run `/mcp` — you should see `purdue-af-agentic-interface` connected with its tools.
4. Ask: **"What's my Purdue AF session status?"**

---

## What you can do

Just ask in plain language, for example:

- "Start my AF session" — or "…with 32 CPUs and the VS Code interface"
- "Wait until it's ready and give me the link"
- "Connect me to my session over SSH"
- "How much home and work storage am I using?"
- "List my Dask clusters" — "scale cluster `<name>` to 10 workers" — "stop it"
- "Show the last 30 minutes of error logs from my notebook"

The same workflows are also available as slash-command prompts in Claude Code:
`/mcp__purdue-af-agentic-interface__launch_session`, `…__connect_session`,
`…__restart_session`, `…__stop_session`, `…__recover_ssh`.

**SSH note:** connecting over SSH runs a couple of local commands (key generation and
`ssh`); Claude shows them for your approval. To pre-approve them in a project, add to
its `.claude/settings.local.json`:

```json
{
  "permissions": {
    "allow": [
      "Bash(ssh PurdueAF *)",
      "Bash(ssh-keygen -t ed25519 -f ~/.ssh/af_key *)"
    ]
  }
}
```

---

## Updating your token

- **Option A (file):** overwrite the file — nothing else to change.
  ```bash
  printf '%s' 'NEW_TOKEN' > ~/.config/purdue-af/token
  ```
- **Option B (client):** `claude mcp remove purdue-af-agentic-interface`, then re-run the
  `claude mcp add` command from step 3 with the new token.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `/mcp` doesn't list the server | Restart Claude Code; if you used a project `.mcp.json`, approve the trust prompt |
| `Invalid JupyterHub token` (HTTP 401) | Token expired or wrong — get a new one at `/hub/token` and update it (see above) |
| `Missing Bearer token` | Token file is empty or the header isn't set — recheck steps 2–3 |
| Tools respond with "no running server found" | Start a session first ("start my AF session") |
| Token file not picked up | Make sure `~/.config/purdue-af/token` exists and contains only the token (no extra spaces) |

---

## Other MCP clients

The endpoint is a standard streamable-HTTP MCP server. Point any MCP client at:

```
https://cms.geddes.rcac.purdue.edu/services/agentic-interface/mcp
```

and send the header `Authorization: Bearer <your-token>`.
