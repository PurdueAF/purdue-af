# self-repair

Cluster side of the [`self-repair`](../../workflows/self-repair) workflow: what
its task pods look like and how the workflow reaches the Flyte control plane
([`apps/flyte`](../flyte)).

| File               | What it is                                                                                         |
| ------------------ | -------------------------------------------------------------------------------------------------- |
| `podtemplate.yaml` | Pod spec of every task pod: AF node placement, the image's user, the GitHub token from a Secret    |
| `deploy-job.yaml`  | `flyte deploy` of the workflow and its trigger, re-run by Flux whenever the code ConfigMap changes |

The code ConfigMap (`self-repair-workflow`) is generated in
[`deploy/experimental/kustomization.yaml`](../../deploy/experimental/kustomization.yaml)
from `workflows/self-repair/*.py` and `config.yaml`, with a content-hash
suffix: that suffix is what turns a merged code change into a new Job.

## Secrets

Created by hand in `cms`; nothing here renders them.

| Secret                 | Key       | Required | Holds                                                                                       |
| ---------------------- | --------- | -------- | ------------------------------------------------------------------------------------------- |
| `self-repair-github`   | `token`   | yes      | Fine-grained GitHub token: `contents:write` + `pull_requests:write` on `PurdueAF/purdue-af` |
| `self-repair-opencode` | `api-key` | no       | OpenCode Zen key, for a paid model                                                          |

The task image is `ghcr.io/purdueaf/self-repair` (`docker/self-repair`), on the
continuous `:latest` channel like the monitor images.
