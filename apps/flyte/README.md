# flyte

[Flyte 2](https://www.union.ai/docs/v2/flyte/) control plane for the workflows
under [`workflows/`](../../workflows).

| File               | What it is                                                                               |
| ------------------ | ---------------------------------------------------------------------------------------- |
| `helmrelease.yaml` | `flyte-binary` chart: one Deployment bundling the API, the task controller and the data proxy |
| `values.yaml`      | Database, object store, and task-pod defaults                                            |
| `postgres.yaml`    | Metadata database (runs, actions, cache)                                                 |
| `minio.yaml`       | Object store for task inputs, outputs and code bundles                                   |
| `podtemplate.yaml` | Base pod spec for every task pod: AF node placement and the `/work` mount                |

Task pods run in `cms` and reach the Dask Gateway, XCache and `/work` like a
notebook does. The API is in-cluster only: `flyte-http.cms.svc.cluster.local:8090`.

Flyte metadata is disposable. Nothing here is backed up.
