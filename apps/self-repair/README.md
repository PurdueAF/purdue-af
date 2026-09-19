# self-repair

Cluster side of the [`self-repair`](../../workflows/self-repair) workflow:
its task pods and the launcher that starts runs on the Flyte control plane
([`apps/flyte`](../flyte)). The task image is
[`docker/self-repair`](../../docker/self-repair).

| File                 | What it is                                                                                 |
| -------------------- | ------------------------------------------------------------------------------------------ |
| `podtemplate.yaml`   | Pod spec of every task pod: AF node placement, the image's user, the keys from the Secrets |
| `cronjob.yaml`       | The launcher: starts one `triage` run per hour, from the code ConfigMap                    |
| `secret-github.yaml` | The GitHub token, sops-encrypted                                                           |
| `secret-genai.yaml`  | The GenAI Studio key, sops-encrypted                                                       |

The experimental root generates two ConfigMaps: `self-repair-workflow` from
`workflows/self-repair/*.py` and `config.yaml`, and
`self-repair-platform-context` from `docker/purdue-af/agents/platform-context.md`.
The launcher mounts the code at every tick and the run registers the tasks it
carries, so a merged code change reaches the next run with no redeploy. The
platform context is mounted into every task pod at the path the purdue-af
image uses.

## Running by hand

An extra tick:

```bash
kubectl -n cms create job --from=cronjob/self-repair self-repair-manual-$(date +%s)
```

Follow a tick with `kubectl -n cms get pods -l flyte.org/project=self-repair`
and `kubectl -n cms logs <pod>`, or in the Flyte console.

## Secrets

Rotating one: [`.claude/rules/manifests.md`](../../.claude/rules/manifests.md).

| Secret                 | Key       | In repo | Holds                                                                                       |
| ---------------------- | --------- | ------- | ------------------------------------------------------------------------------------------- |
| `self-repair-github`   | `token`   | yes     | Fine-grained GitHub token: `contents:write` + `pull_requests:write` on `PurdueAF/purdue-af` |
| `self-repair-genai`    | `api-key` | yes     | Purdue GenAI Studio key (Settings → Account → API Keys), acts as its owner                  |
| `self-repair-opencode` | `api-key` | no      | OpenCode Zen key, created by hand, only for an `opencode/*` model                           |
