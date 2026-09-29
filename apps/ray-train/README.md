# ray-train

A gateway that gives every AF user a Ray cluster of their own for
[Ray Train](https://docs.ray.io/en/latest/train/train.html) jobs. The Ray
Jobs CLI in a session talks to the gateway with the session's JupyterHub
token; the gateway asks the Hub whose token it is, and forwards the call to
that user's cluster: one pod with one T4, created when they first send code or
a job, running as them, and deleted once idle. How users run a job,
not yet on the documentation site:
[Training on GPUs with Ray Train](../../docs/drafts/guide-ray-train.md).

| File                 | What it is                                                                                                   |
| -------------------- | ------------------------------------------------------------------------------------------------------------ |
| `gateway.py`         | The gateway: forwards the Ray Jobs API, creates and deletes users' RayClusters                               |
| `raycluster.yaml`    | The template of a user's cluster; the gateway fills in its name, the user's UID/GID and its token Secret     |
| `deployment.yaml`    | The gateway pod: the stock Ray image, with both files above from the `ray-train-gateway` ConfigMap           |
| `service.yaml`       | `ray-train-gateway:8265`, the address the sessions' Ray CLI is given                                         |
| `rbac.yaml`          | Create, read and delete RayClusters; create their token Secrets                                              |
| `networkpolicy.yaml` | Only sessions reach the gateway, and only the gateway reaches users' clusters                                |

The session side is in the Hub config:
[`extraFiles/ray-train.py`](../jupyterhub/jupyterhub/extraFiles/ray-train.py)
points every session's `ray job` at the gateway with the session's token, and
[`values.yaml`](../jupyterhub/jupyterhub/values.yaml) registers the
`ray-train-gateway` service, whose Hub token the gateway reads from the `hub`
Secret. The `ray.io` CRDs and their controller are the KubeRay operator in
[`apps/ray/operator`](../ray/operator).

- **Identity**: the Hub resolves the token to a user and the gateway's own
  token (`read:servers`, `admin:server_state`) reads the pod name of their
  session, `purdue-af-<id>`. The cluster is `ray-train-<id>` and runs as the
  user's LDAP account: their Purdue login, or the pooled `paf<id>` account of
  a CERN or FNAL user, as in
  [`set-user-info.py`](../jupyterhub/jupyterhub/extraFiles/set-user-info.py).
- **Isolation**: each cluster has its own Ray token, an HMAC of its name under
  the gateway's Hub token; only the gateway sends it. Users' pods mount no
  ServiceAccount token, and have no autoscaler, whose Role would let user code
  read every pod in `cms` and patch every RayCluster.
- **Lifetime**: a cluster with no pending or running job for `IDLE_TIMEOUT_S`
  is deleted, with its job history; its token Secret goes with it.
- **Before the Hub registers the service** the gateway answers every call with
  503. Its token then appears in the mounted `hub` Secret, which the gateway
  reads on every call, so no restart is needed.

## Checking on it

```bash
kubectl -n cms get rayclusters -l app.kubernetes.io/managed-by=ray-train-gateway
kubectl -n cms logs deploy/ray-train-gateway
```
