# Krab

[Krab](https://gitlab.cern.ch/bgalewsk/krab) — Dagster driving per-run Celery worker pools over
RabbitMQ and PostgreSQL. The chart, its design and its CERN NGT defaults are documented upstream
in [`helm/README.md`](https://gitlab.cern.ch/bgalewsk/krab/-/blob/master/helm/README.md).

| File | Contents |
| --- | --- |
| `gitrepo.yaml` | the upstream repository, branch `master` |
| `helmrelease.yaml` | the `krab` release of its `helm/` chart |
| `values.yaml` | overrides of the chart's NGT values for Geddes |
| `secret-*.yaml` | SOPS-encrypted PostgreSQL, RabbitMQ and Celery connection Secrets |

Flux deploys it from [`flux/experimental/`](../../flux/experimental/kustomization.yaml). Nothing
is exposed outside the cluster; reach the UIs by port-forward:

```bash
kubectl -n cms port-forward svc/krab-dagster-webserver 8080:3000
kubectl -n cms port-forward svc/krab-rabbitmq 15672:15672
```

Upstream commits to `master` reach the cluster on the next reconcile; `values.yaml` overrides the
upstream `helm/values.yaml`, so a change there can need one here.

## CERN credentials

The control plane runs without them. A `cmssw_run` needs three Secrets that hold personal CERN
credentials, which are not in this repository:

| Secret | Enables |
| --- | --- |
| `gitlab-cern-registry` | pulling the private `cmssw-docker` worker image; the new-image sensor |
| `kerberos-credentials` | reading CERN EOS, with `celery.worker.kerberos.enabled: true` |
| `mlflow-credentials` | the MLflow record of each run, once listed in the code location's `envSecrets` |

Their keys are in the upstream chart README. Add each as a SOPS Secret here and list it in
`flux/experimental/kustomization.yaml`.
