# jfc

Cluster side of the [`jfc`](../../workflows/jfc) workflow: its task pods and
the directory they work in. Runs are started by hand
([Running](../../workflows/jfc/README.md#running)) on the Flyte control plane
([`apps/flyte`](../flyte)).

| File               | What it is                                                                                                                                                                            |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `podtemplate.yaml` | Pod spec of every task pod: AF node placement, the image's user, the model credentials, where profiles go, the Dask Gateway, the facility's grid proxy and CVMFS, the workspace mount |
| `workspace.yaml`   | The Job that creates `/work/projects/jfc` for that user                                                                                                                               |

## Secrets

| Secret       | In repo | Holds                                                                                                   |
| ------------ | ------- | ------------------------------------------------------------------------------------------------------- |
| `jfc-claude` | no      | Created by hand. Each key becomes an environment variable of the task pods, named as its reader expects |

One of these keys:

- `CLAUDE_CODE_OAUTH_TOKEN`, as printed by `claude setup-token`: runs count
  against that account's subscription
- `ANTHROPIC_API_KEY`: runs are billed to the API account

And `JUPYTERHUB_API_TOKEN`, a token from the hub's Token page: the analyses'
Dask and Ray clusters are its owner's, run as that user and count as that
user's one cluster of each.
