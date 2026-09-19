# flyte

[Flyte 2](https://www.union.ai/docs/v2/flyte/) control plane for the workflows
under [`workflows/`](../../workflows).

| File                       | What it is                                                                                    |
| -------------------------- | --------------------------------------------------------------------------------------------- |
| `helmrelease.yaml`         | `flyte-binary` chart: one Deployment bundling the API, the task controller and the data proxy |
| `values.yaml`              | Database, object store, and task-pod defaults                                                 |
| `postgres.yaml`            | Metadata database (runs, actions, cache)                                                      |
| `minio.yaml`               | Object store for task inputs, outputs and code bundles, and the Job that creates its bucket   |
| `podtemplate.yaml`         | Base pod spec for every task pod: AF node placement and the `/work` mount                     |
| `secret-console-auth.yaml` | Basic-auth credentials in front of the console and API ingress, sops-encrypted                |

Task pods run in `cms` and reach the Dask Gateway, XCache and `/work` like a
notebook does. In-cluster, the API is `flyte-http.cms.svc.cluster.local:8090`.

The ingress serves the console at `https://flyte-cms.geddes.rcac.purdue.edu/v2`
behind basic auth (user `af`; the password is the one encrypted in
`secret-console-auth.yaml`); the host name is resolvable only once RCAC has a
DNS record for it. Without it, from a laptop, forward `svc/flyte-console`
(port 80) and `svc/flyte-http` (8090) and put both behind one local origin (the
console calls the API on its own origin), or use the `flyte` CLI against the
API forward with `endpoint: dns:///localhost:<port>`.

Flyte metadata is disposable. Nothing here is backed up.
