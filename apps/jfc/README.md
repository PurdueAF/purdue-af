# jfc

Cluster side of the [`jfc`](../../workflows/jfc) workflow: its task pods and
the directory they work in. Runs are started by hand
([Running](../../workflows/jfc/README.md#running)) on the Flyte control plane
([`apps/flyte`](../flyte)).

| File               | What it is                                                                                                  |
| ------------------ | ----------------------------------------------------------------------------------------------------------- |
| `podtemplate.yaml` | Pod spec of every task pod: AF node placement, the image's user, the model credentials, the workspace mount |
| `workspace.yaml`   | The Job that creates `/work/projects/jfc` for that user                                                     |

## Secrets

| Secret       | In repo | Holds                                                                                                     |
| ------------ | ------- | --------------------------------------------------------------------------------------------------------- |
| `jfc-claude` | no      | Created by hand. Each key becomes an environment variable of the task pods, named as Claude Code reads it |

One of these sets of keys:

- `ANTHROPIC_API_KEY`
- `CLAUDE_CODE_OAUTH_TOKEN`, as printed by `claude setup-token`
- `ANTHROPIC_BASE_URL` and `ANTHROPIC_AUTH_TOKEN` for a gateway, with
  `ANTHROPIC_DEFAULT_OPUS_MODEL` naming the model it serves
