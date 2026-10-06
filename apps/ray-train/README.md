# ray-train

A gateway that gives every AF user a Ray cluster of their own. A notebook
connects with Ray Client, `ray.init("ray://ray-train-gateway:10001")`, or
submits jobs with the Jobs API's client,
`JobSubmissionClient("http://ray-train-gateway:8265")`, signed in with its
session's JupyterHub token either way; the gateway asks the Hub whose token it
is and relays the calls, unread, to that user's cluster: a head and
workers with a GPU each, one unless a call asks for more, created when they
first connect or submit a job, running as them, and deleted once idle. A
worker's GPU is a 5 GB A100 slice or a T4, whichever is free, slices first;
a call that asks for more GPU memory than a slice has gets T4s only. The
first worker keeps its GPU; the others run in the `low-priority-preemptible`
PriorityClass, which the scheduler evicts for a pod of the default priority
that fits nowhere else: a session, to which the Hub's profile form shows such
a worker's GPU as free, or another cluster's first worker. A cluster starts
only while the GPUs it asks for are free, by the queries of that form
([`gpu_queries.py`](../jupyterhub/jupyterhub/extraFiles/gpu_queries.py), in the
gateway's ConfigMap too); one that asks for a single GPU also starts while a
preemptible worker holds one, which its worker evicts. How users
send their training to it, not on the documentation site:
[Training on GPUs with Ray](../../docs/drafts/guide-ray-train.md).

| File                 | What it is                                                                                                                |
| -------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| `gateway.py`         | The gateway: relays Ray Client and Jobs API calls, creates and deletes users' RayClusters                                 |
| `config.yaml`        | The gateway's settings: timeouts, GPU limits, and the addresses it asks                                                   |
| `raycluster.yaml`    | The template of a user's cluster; the gateway fills in its name, the user's UID/GID, its environment, its token Secret and its worker groups, one per kind of GPU and priority |
| `deployment.yaml`    | The gateway pod: the stock Ray image, with the files above from the `ray-train-gateway` ConfigMap                         |
| `service.yaml`       | `ray-train-gateway:10001` for Ray Client and `:8265` for the Jobs API, the addresses notebooks connect to                 |
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

A cluster runs as the user's LDAP account, in its notebook's environment,
since Ray Client needs the same Python and Ray on both sides: the global Pixi
environment that [`pixi-global-sync`](../af-utils/pixi-global-sync) keeps, or
the one a call names. Only the gateway reaches it, with a Ray
token derived for that cluster alone, and its pods hold no Kubernetes
credentials.

## Tuning

[`config.yaml`](config.yaml) holds the gateway's settings, each with what it
does: the timeouts, the PriorityClass of the preemptible workers, and `gpus`,
the kinds of GPU a worker may hold, each with its memory and with the most of
them all clusters hold together while Prometheus does not answer. `raycluster.yaml` holds the pods' sizes, mounts and image. The
image's tag is also in `deployment.yaml` and in the Hub's
`prePuller.extraImages`.

## Running it

A change to `raycluster.yaml` applies to the clusters created after it lands.
One to `gateway.py` or `config.yaml` applies once kubelet swaps it into the
pod's copy of the ConfigMap: the gateway sees the file change and exits, and
kubelet restarts its container, in the same pod, on the new file. Each such change adds one to
the container's restart count, and the previous container's log ends with the
line that names it. Unless the gateway is back before Ray Client stops
retrying, a restart ends notebooks' Ray Client connections and the calls
running over them, but not their jobs.

```bash
kubectl -n cms get rayclusters -l app.kubernetes.io/managed-by=ray-train-gateway
kubectl -n cms get pod -l app=ray-train-gateway
kubectl -n cms logs deploy/ray-train-gateway
kubectl -n cms logs deploy/ray-train-gateway --previous
```
