# self-repair

Cluster side of the [`self-repair`](../../workflows/self-repair) workflow:
what its task pods look like and how runs get started on the Flyte control
plane ([`apps/flyte`](../flyte)).

| File                 | What it is                                                                                      |
| -------------------- | ----------------------------------------------------------------------------------------------- |
| `podtemplate.yaml`   | Pod spec of every task pod: AF node placement, the image's user, the GitHub token from a Secret |
| `cronjob.yaml`       | The launcher: starts one `triage` run per tick, from the code ConfigMap. Suspended for now      |
| `secret-github.yaml` | The GitHub token, sops-encrypted                                                                |
| `secret-genai.yaml`  | The GenAI Studio key, sops-encrypted                                                            |

The code ConfigMap (`self-repair-workflow`) is generated here from
`workflows/self-repair/*.py` and `config.yaml`. The launcher mounts it at
every tick, so a merged code change reaches the next run with no redeploy:
the run registers the tasks it carries.

## Running by hand

The CronJob is `suspend: true` while the workflow is being tested. One tick:

```bash
kubectl -n cms create job --from=cronjob/self-repair self-repair-manual-$(date +%s)
```

Every run and pod is named after its task:
`self-repair-triage-<tick>`, `self-repair-watch-<tick>`,
`self-repair-analyze-<tick>-<fp>`, `self-repair-fix-<tick>-<fp>`, where the
tick is the launch minute in base36 (5 characters; run names are capped at 30)
and `fp` the first 4 of the fingerprint; the pod of each is `<run>-a0-0`. Follow along with
`kubectl -n cms get pods -l flyte.org/project=self-repair` and
`kubectl -n cms logs <pod>`, or in the Flyte console.

To go live, set `suspend: false` and pick the `schedule`.

## Secrets

`secret-github.yaml` is sops-encrypted with the repository's age recipient
(`.sops.yaml`) and decrypted by the experimental Flux Kustomization. To rotate:
`sops apps/self-repair/secret-github.yaml`, replace the value, run prettier on
the file, commit. On macOS point sops at the key first:
`export SOPS_AGE_KEY_FILE=~/.config/sops/age/keys.txt`.
The OpenCode key is created by hand only when a paid model is configured.

| Secret                 | Key       | In repo | Holds                                                                                       |
| ---------------------- | --------- | ------- | ------------------------------------------------------------------------------------------- |
| `self-repair-github`   | `token`   | yes     | Fine-grained GitHub token: `contents:write` + `pull_requests:write` on `PurdueAF/purdue-af` |
| `self-repair-genai`    | `api-key` | yes     | Purdue GenAI Studio key (Settings → Account → API Keys), acts as its owner                  |
| `self-repair-opencode` | `api-key` | no      | OpenCode Zen key, only for an `opencode/*` model                                            |

The task image is `ghcr.io/purdueaf/self-repair` (`docker/self-repair`), on the
continuous `:latest` channel like the monitor images.
