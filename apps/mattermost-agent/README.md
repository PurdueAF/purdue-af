# mattermost-agent

PurdueAF Agent, a service account on
[CERN Mattermost](https://mattermost.web.cern.ch), acting through an OAuth 2.0
application. It
answers an @mention in one public channel, in a thread, later replies in that
thread that mention nobody else, and a direct message from a member of that
channel, from the user docs and the
[agentic interface](../agentic-interface/README.md)'s facility-wide tools, with
a role of the [LLM proxy](../agentic-analysis/README.md#llm-proxy).

| File                 | What it is                                                                    |
| -------------------- | ----------------------------------------------------------------------------- |
| `agent.py`           | The bot: polls its channels, answers mentions, caps each user's questions, serves the login |
| `system-prompt.md`   | What the model is told; read on each question                                 |
| `deployment.yaml`    | Runs `agent.py` from the `mattermost-agent` ConfigMap, the docs from `mattermost-agent-docs` |
| `networkpolicy.yaml` | Ingress from the Hub's proxy; egress to the agentic interface, the LLM proxy and HTTPS outside |
| `pvc.yaml`           | The volume holding the Mattermost login and the linked people's tokens        |
| `service.yaml`       | The route the Hub's proxy sends the login to                                  |
| `secret.yaml`        | The `mattermost-agent` Secret, SOPS-encrypted: the OAuth application's `client-id` and `client-secret`, the service account's `username`, and `link-key`, the Fernet key of the linked tokens |

## Identity

The bot is the Hub service `mattermost-agent`, with no role, so the MCP server
sees a caller with no session, storage or clusters. `FACILITY_TOOLS` in
`agent.py` lists the tools it offers the model. It sends the model the
conversation's text, never who wrote it, and a reply cannot mention anyone.

## Linking

A person links their account by sending `link` in a direct message. The link works
once, for 24 hours. It opens a Hub login that shows a code, and the link is made only
when that code comes back in a direct message from the same Mattermost user. Linking
mints a 30-day Hub token noted "PurdueAF Agent on Mattermost" and retires that
user's older ones. A person sends `link` again when it runs out, and `unlink` to stop;
`unlink` forgets the token, which the Hub lists on its token page until it expires.

Tokens are encrypted in `/state/links.enc` under `link-key`, keyed by Mattermost user id.
`/metrics`, reachable only inside the cluster, gives how many accounts are linked.

Each question first goes to the model alone with its author's own posts, to decide
whether they ask about their own things. If so, it runs with the author's token and
`PERSONAL_TOOLS`, and the model reads only the author's posts and the bot's replies;
otherwise it runs as above. The bot replies where it was asked.

## Running it

`MATTERMOST_TEAM` and `MATTERMOST_CHANNEL` in `deployment.yaml` name the channel,
by the display names Mattermost shows, and its account must belong to it. Each
direct message checks the sender's membership at that moment, so leaving the
channel ends access. Removing the account from the channel stops the bot in
both. In a group message it answers an @mention, when everyone there is a member
at that moment. A new `docs/docs/` page is added to
`mattermost-agent-docs` in [`flux/experimental`](../../flux/experimental/kustomization.yaml).

Tuning, all in `agent.py`: `POLL_SECONDS`, `USER_LIMIT` and
`USER_WINDOW_SECONDS`, `CONCURRENT_ANSWERS`, `MAX_TURNS`. The prompt and the
docs apply on the next question; a change to `agent.py` applies when its pod is
deleted, and a change to the role in `models.toml` when the LLM proxy's is.

People mention the bot by the service account's username.

## Authorizing

Mattermost replaces the refresh token on every use, so the tokens live in
`/state/oauth.json`. The agent starts with none, and returns to that state when
Mattermost refuses them: it waits for a login, and answers nothing until then.

Create the service account, add it to the channel, and register an OAuth 2.0
application with the callback URL
`https://cms.geddes.rcac.purdue.edu/services/mattermost-agent/callback`. Set
`client-id`, `client-secret` and `username`, the service account's Mattermost
username, in the Secret:

```bash
sops apps/mattermost-agent/secret.yaml
```

Once that is deployed, log in as the service account, in a private window, at
<https://cms.geddes.rcac.purdue.edu/services/mattermost-agent/>. Anyone can reach
that page, but the agent keeps a login only when `username` made it.
