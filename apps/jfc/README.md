# jfc

Cluster side of the [`jfc`](../../workflows/jfc) workflow: its task pods and
the directory they work in. Runs are started by hand
([Running](../../workflows/jfc/README.md#running)) on the Flyte control plane
([`apps/flyte`](../flyte)).

| File               | What it is                                                                                                  |
| ------------------ | ----------------------------------------------------------------------------------------------------------- |
| `podtemplate.yaml` | Pod spec of every task pod: AF node placement, the image's user, the GenAI Studio key, the workspace mount  |
| `workspace.yaml`   | The Job that creates `/work/projects/jfc` for that user                                                     |

The key is the `self-repair-genai` Secret of
[`apps/self-repair`](../self-repair/README.md#secrets).
