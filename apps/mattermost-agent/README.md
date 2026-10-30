# mattermost-agent

PurdueAF Agent, a service account on
[CERN Mattermost](https://mattermost.web.cern.ch), acting through an OAuth 2.0
application. It
answers an @mention in one public channel, in a thread, and a direct message
from a member of that channel, from the user docs and the
[agentic interface](../agentic-interface/README.md)'s facility-wide tools, with
a role of the [LLM proxy](../agentic-analysis/README.md#llm-proxy).

| File                 | What it is                                                                    |
| -------------------- | ----------------------------------------------------------------------------- |
| `agent.py`           | The bot: polls its channels, answers mentions, caps each user's questions     |
| `system-prompt.md`   | What the model is told; read on each question                                 |
| `deployment.yaml`    | Runs `agent.py` from the `mattermost-agent` ConfigMap, the docs from `mattermost-agent-docs` |
| `networkpolicy.yaml` | No ingress; egress to the agentic interface, the LLM proxy and HTTPS outside  |
| `pvc.yaml`           | The volume holding the current OAuth tokens                                   |
| `authorize.py`       | Run by hand, from a checkout: the service account's one browser login         |
| `secret.yaml`        | The `mattermost-agent` Secret, SOPS-encrypted: the OAuth application's `client-id` and `client-secret`, and the first `refresh-token` |

## Identity

The bot is the Hub service `mattermost-agent`, with no role, so the MCP server
sees a caller with no session, storage or clusters. `FACILITY_TOOLS` in
`agent.py` lists the tools it offers the model. It sends the model the
conversation's text, never who wrote it, and a reply cannot mention anyone.

## Running it

`MATTERMOST_TEAM` and `MATTERMOST_CHANNEL` in `deployment.yaml` name the channel,
by the display names Mattermost shows, and its account must belong to it. Each
direct message checks the sender's membership at that moment, so leaving the
channel ends access. Removing the account from the channel stops the bot in
both. Group messages are not answered. A new `docs/docs/` page is added to
`mattermost-agent-docs` in [`flux/experimental`](../../flux/experimental/kustomization.yaml).

Tuning, all in `agent.py`: `POLL_SECONDS`, `USER_LIMIT` and
`USER_WINDOW_SECONDS`, `CONCURRENT_ANSWERS`, `MAX_TURNS`. The prompt and the
docs apply on the next question; a change to `agent.py` applies when its pod is
deleted, and a change to the role in `models.toml` when the LLM proxy's is.

People mention the bot by the service account's username.

## Authorizing

Mattermost replaces the refresh token on every use, so the current one lives in
`/state/oauth.json`, and `refresh-token` in the Secret only starts the chain. A
new value there takes over from the file. The pod exits with `Unauthorized` when
Mattermost refuses the refresh token.

Register an OAuth 2.0 application in Mattermost, with the callback URL
`http://localhost:8765/callback` and without "Is Trusted", then:

```bash
SOPS_AGE_KEY_FILE=~/.config/sops/age/keys.txt sops apps/mattermost-agent/secret.yaml
```

Set `client-id` and `client-secret`, save, and log the service account in:

```bash
SOPS_AGE_KEY_FILE=~/.config/sops/age/keys.txt uv run --no-project --with httpx python apps/mattermost-agent/authorize.py
```

Commit `secret.yaml`; its pod loads a new value when deleted.
