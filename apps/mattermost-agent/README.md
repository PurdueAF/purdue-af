# mattermost-agent

PurdueAF Agent, a service account on
[CERN Mattermost](https://mattermost.web.cern.ch), acting through an OAuth 2.0
application. It
answers an @mention in one channel, in a thread, later replies in that
thread that mention nobody else, direct messages, and @mentions in group
messages, from the user docs and the
[agentic interface](../agentic-interface/README.md)'s facility-wide tools, with
a role of the [LLM proxy](../agentic-analysis/README.md#llm-proxy).

| File                 | What it is                                                                    |
| -------------------- | ----------------------------------------------------------------------------- |
| `agent.py`           | The bot: polls its channels, answers mentions, caps each user's questions, counts its answers and their thumbs, serves the login |
| `system-prompt.md`   | What the model is told; read on each question                                 |
| `deployment.yaml`    | Runs `agent.py` from the `mattermost-agent` ConfigMap, the docs from `mattermost-agent-docs` |
| `networkpolicy.yaml` | Ingress from the Hub's proxy; egress to the agentic interface, the LLM proxy and HTTPS outside |
| `pvc.yaml`           | The volume holding the Mattermost login and the linked people's tokens        |
| `service.yaml`       | The route the Hub's proxy sends the login to                                  |
| `secret.yaml`        | The `mattermost-agent` Secret, SOPS-encrypted: the OAuth application's `client-id` and `client-secret`, the service account's `username`, and `link-key`, the Fernet key of the linked tokens |

## Identity

The bot is the Hub service `mattermost-agent`, with no role, so the MCP server
sees a caller with no session, storage or clusters. The model gets the MCP
server's own guidance and the tools the server marks as both read-only and
answering for the facility. It sends the model the conversation's text, never who
wrote it, and a reply cannot mention anyone.

## Linking

A person links their account by asking for it in a direct message, in any
words, or with `link`. The link works
once, for 24 hours. It opens a Hub login that shows a code, and the link is made only
when that code comes back in a direct message from the same Mattermost user. Linking
mints a 30-day Hub token noted "PurdueAF Agent on Mattermost" and retires that
user's older ones. A person asks again when it runs out, and asks to unlink, or sends `unlink`, to stop;
`unlink` forgets the token, which the Hub lists on its token page until it expires.

Tokens are encrypted in `/state/links.enc` under `link-key`, keyed by Mattermost user id.

Each question first goes to the model alone with its author's own posts, to decide
whether they ask to link, to unlink, or about their own things, read with the
bot's replies. If about their own things, it runs with the author's token, every
tool but those in `WITHHELD` is offered, and the model reads only the author's
posts and the bot's replies; otherwise it runs as above. The bot replies where it
was asked.

## Feedback

A thumbs-up or thumbs-down that someone leaves on an answer is counted. Any other
reaction is not, nor is one on a failure notice or a linking reply. For each thumb
the count is the most people who held it at once, so one taken back and put again
adds nothing, and who reacted is kept nowhere. The bot follows the last
`MAX_FOLLOWED` answers it gave since its pod started.

The first thumbs-down on an answer has the model say why it failed, from the
conversation, the answer and the names of the tools that ran:

| Cause       | The answer failed because                                    |
| ----------- | ------------------------------------------------------------ |
| `knowledge` | the docs and the tools do not hold what was asked            |
| `retrieval` | they hold it, and the wrong page or tool was read, or none   |
| `tool`      | a tool failed or returned something wrong                    |
| `model`     | the model had what it needed and answered badly              |
| `question`  | the question is not about the facility, or is unclear        |
| `unknown`   | the check itself failed                                      |

The log line `post <id>: thumbs-down, cause=<cause>` names the question's post,
which `<MATTERMOST_URL>/<MATTERMOST_TEAM>/pl/<id>` opens for anyone who can read that
conversation. A question worth keeping is added to `evaluate.yaml` by hand.

## Metrics

`/metrics`, reachable only inside the cluster:

| Series                                                 | Counts                                                    |
| ------------------------------------------------------ | --------------------------------------------------------- |
| `purdue_af_mattermost_linked_accounts`                 | Accounts linked now                                       |
| `purdue_af_mattermost_answers_total{intent, outcome}`  | Questions, by what was asked and how it ended             |
| `purdue_af_mattermost_feedback_total{reaction}`        | Thumbs on answers, `up` and `down`                        |
| `purdue_af_mattermost_thumbs_down_total{cause}`        | Answers that got a thumbs-down, by the cause above        |

`intent` is `general`, `personal`, `link` or `unlink`, and `unknown` for a question
that ended before it was known. `outcome` is `answered` or the failure posted in
its place, one of `OUTCOMES` in `agent.py`.

## Evaluating

`evaluate.py` asks the questions in `evaluate.yaml` through the bot's own answer
path, against the live agentic interface and LLM proxy, and reports which answers
pass. It runs from a checkout; `--help` lists its options. It needs the LLM proxy
forwarded, and its client token and the bot's Hub token in files outside the
checkout:

```bash
kubectl -n cms port-forward svc/agentic-analysis-llm-proxy 8080
```

```bash
SOPS_AGE_KEY_FILE=~/.config/sops/age/keys.txt sops --decrypt --extract '["stringData"]["client-token"]' apps/agentic-analysis/secret-llm-proxy.yaml > ~/.config/purdue-af/llm-token
```

```bash
kubectl -n cms get secret hub -o go-template='{{index .data "hub.services.mattermost-agent.apiToken" | base64decode}}' > ~/.config/purdue-af/bot-token
```

```bash
uv run --project tests --frozen python apps/mattermost-agent/evaluate.py --llm-token-file ~/.config/purdue-af/llm-token --token-file ~/.config/purdue-af/bot-token
```

General questions call the agentic interface as the bot; the cases marked `linked`
call it with the person's token of `--person-token-file`. The live server must
already serve the tool annotations and scopes the bot reads.

## Running it

`MATTERMOST_TEAM` and `MATTERMOST_CHANNEL` in `deployment.yaml` name the channel,
by the names in its URL; the log says why when it cannot have it. The bot
answers anyone who writes to it; personal answers need a linked account. A new `docs/docs/` page is added to
`mattermost-agent-docs` in [`flux/experimental`](../../flux/experimental/kustomization.yaml).

Tuning, all in `agent.py`: `POLL_SECONDS`, `USER_LIMIT` and
`USER_WINDOW_SECONDS`, `CONCURRENT_ANSWERS`, `MAX_TURNS`, `MAX_FOLLOWED`. The prompt and the
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
