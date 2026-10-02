# ray-train

A gateway that gives every AF user a Ray cluster of their own. A notebook
connects with Ray Client, `ray.init("ray://ray-train-gateway:10001")`, signed
in with its session's JupyterHub token; the gateway asks the Hub whose token it
is and relays the calls, unread, to that user's cluster: a head and one to four
workers with a T4 each, created when they first connect, running as them, and
deleted once idle. How
users send their training to it, not yet on the documentation site:
[Training on GPUs with Ray](../../docs/drafts/guide-ray-train.md).

| File                 | What it is                                                                                                                |
| -------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| `gateway.py`         | The gateway: relays Ray Client calls, creates and deletes users' RayClusters                                              |
| `raycluster.yaml`    | The template of a user's cluster; the gateway fills in its name, the user's UID/GID, its environment and its token Secret |
| `deployment.yaml`    | The gateway pod: the stock Ray image, with both files above from the `ray-train-gateway` ConfigMap                        |
| `service.yaml`       | `ray-train-gateway:10001`, the address notebooks connect to                                                               |
| `rbac.yaml`          | Create, read and delete RayClusters; create their token Secrets                                                           |
| `networkpolicy.yaml` | Only sessions reach the gateway, and only the gateway reaches users' clusters                                             |

The session side is in the Hub config:
[`extraFiles/ray-train.py`](../jupyterhub/jupyterhub/extraFiles/ray-train.py)
gives every session `RAY_AUTH_MODE=token` and its own token as
`RAY_AUTH_TOKEN`, which Ray Client sends with every call, and
[`values.yaml`](../jupyterhub/jupyterhub/values.yaml) registers the
`ray-train-gateway` service, whose Hub token the gateway reads from the `hub`
Secret. The `ray.io` CRDs and their controller are the KubeRay operator in
[`apps/ray/operator`](../ray/operator).

- **Identity**: the Hub resolves the token to a user, whose cluster is
  `ray-train-<username>` (a hashed form for a username that cannot be part of
  a Kubernetes name) and runs as their LDAP account: their Purdue login, or
  for a CERN or FNAL user the pooled `paf<id>` account of
  [`set-user-info.py`](../jupyterhub/jupyterhub/extraFiles/set-user-info.py),
  whose `<id>` the gateway's own token (`read:servers`, `admin:server_state`)
  reads from the pod name of their session, `purdue-af-<id>`.
- **Isolation**: each cluster has its own Ray token, an HMAC of its name under
  the gateway's Hub token, which the gateway puts on every call it relays in
  place of the session's. The NetworkPolicy KubeRay writes for each cluster
  (`networkPolicy` in the template, with the operator's
  `RayClusterNetworkPolicy` gate) admits only the cluster's own pods, beside
  the gateway. Users' pods mount no ServiceAccount token and have no
  autoscaler, whose Role would let user code read every pod in `cms` and patch
  every RayCluster.
- **Up to four GPUs per user**: a user's cluster is their only one, named after
  them. Its head runs Ray's own processes and the notebooks' connections, with
  no GPU and no tasks (`num-cpus: 0`); its workers hold a T4 each, as many as
  the `af-ray-gpus` metadata asks, from 1 to 4, one by default. The gateway
  deletes a replaced or idle cluster in the foreground and waits until it is
  gone, pods included, before creating the next.
- **Environment**: Ray Client needs the same Python and Ray on both sides, so
  a cluster runs its notebook's environment: the global Pixi environment that
  [`pixi-global-sync`](../af-utils/pixi-global-sync) keeps, whose
  [`pixi.toml`](../../pixi/global/pixi.toml) has `ray-default` and `ray-train`
  for it, or the one a notebook names in the `af-ray-env` metadata, on storage
  the cluster mounts. The environment's `bin/` comes first on the cluster's
  `PATH`. A connection naming another environment replaces a cluster that runs
  no task.
- **Lifetime**: the first call of a connection waits while the cluster
  starts. A cluster that runs no task and hears nothing from its notebooks for
  `IDLE_TIMEOUT_S` is deleted, with its token Secret; the logs it streams back
  do not count. Its notebooks' reconnects are then refused rather than given a
  new cluster.
- **Before the Hub registers the service** the gateway refuses every call as
  unavailable. Its token then appears in the mounted `hub` Secret, which the
  gateway reads on every call, so no restart is needed.

## Checking on it

```bash
kubectl -n cms get rayclusters -l app.kubernetes.io/managed-by=ray-train-gateway
kubectl -n cms logs deploy/ray-train-gateway
```
