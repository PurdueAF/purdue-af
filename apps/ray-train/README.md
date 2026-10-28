# ray-train

A gateway that gives every AF user a Ray cluster of their own, and every job
they submit a cluster of its own. A notebook connects with Ray Client,
`ray.init("ray://ray-train-gateway:10001")`, or submits jobs with the Jobs
API's client, `JobSubmissionClient("http://ray-train-gateway:8265")`, signed
in with its session's JupyterHub token either way; the gateway asks the Hub
whose token it is and relays the calls, unread. Ray Client's reach that user's
RayCluster: a head and workers, one with one GPU unless a call asks for more
of either, created when they first connect, running as them, and deleted once
idle. A submission, and the uploads of the code it takes along, reach a RayJob
of its own, `ray-job-<hash>`, labelled `purdue-af/ray-owner` with the user's
cluster's name: KubeRay makes its cluster as the gateway makes a user's, the
gateway submits the job to that cluster's head, copies the end of the job's logs
into a ConfigMap `ray-job-<hash>-logs` the RayJob owns once the
job ends, then suspends its workers, which frees their GPUs, and KubeRay
removes its head `idleTimeoutSeconds` after. A call about a job reaches its
cluster while that is up, and its RayJob's record of it afterwards, with its
logs from the ConfigMap, until `jobRecordSeconds` after it ended; a job whose workers do not all start within
`provisionTimeoutSeconds` is stopped. The RayJob is created at the first call
of a submission, a package upload or the submission itself, and waits; the
gateway then sets its `jobId` to the submission ID the client named, or one it
makes, and KubeRay follows the job of that ID. A
worker's GPU is a 5 GB A100 slice or a T4, whichever is free, slices first;
a call that asks for more GPU memory than a slice has, or for two GPUs a
worker, gets T4s only. A call
may also name each worker's CPU cores and memory. The
first worker keeps its GPUs; the others run in the `medium-priority-preemptible`
PriorityClass, for which the scheduler evicts Dask Gateway's preemptible
workers, and which it evicts for a pod of the default priority
that fits nowhere else: a session, to which the Hub's profile form shows such
a worker's GPU as free, or another cluster's first worker. A cluster that asks
for a number of workers starts only while the GPUs of them all are free, by
the queries of that form
([`gpu_queries.py`](../jupyterhub/jupyterhub/extraFiles/gpu_queries.py), in the
gateway's ConfigMap too), and one that asks for up to a number, with those
whose GPUs are free; one that a single GPU starts also starts while a
preemptible worker holds one, which its worker evicts. A cluster's pods read
the most workers it has from `AF_MAX_WORKERS`, by which a training sizes
itself. A user whose session
holds a GPU, by the Hub's name of its pod and that pod's requests in
Prometheus, starts and submits to none. A
[workload account](../dask-gateway/README.md#workload-accounts) signs in with
its own token and gets a cluster of its own, `ray-train-<name>`. How users
send their training to it:
[Training on GPUs with Ray](../../docs/docs/guide-ray-train.md).

| File                 | What it is                                                                                                                |
| -------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| `gateway.py`         | The gateway: relays Ray Client and Jobs API calls, creates and deletes users' RayClusters, and creates a RayJob per submission |
| `config.yaml`        | The gateway's settings: timeouts, GPU limits, the addresses it asks, and the volumes a workload account's cluster keeps    |
| `nrp_gpus.py`        | How many GPUs NRP Nautilus has free for Ray workers, by region, from NRP's public Prometheus; `GET /af/nrp-gpus` on the gateway's `:8265` answers it |
| `rayjob-nrp.yaml`    | The template of a job on NRP: its image, its head's and worker's sizes, its NCCL settings, and the nrp-s3 keys its pods read |
| `nrp_jobs.py`        | A job on NRP: its RayJob, the NRP API it is made through, and the S3 prefix of its code and data |
| `stage.py`           | What a job's `<RayJob>-in` and `-out` Jobs run: copies its inputs to S3, and its outputs back |
| `raycluster.yaml`    | The template of a user's cluster, and of a job's; the gateway fills in its name, the user's UID/GID, its environment, its token Secret and its worker groups, one per kind of GPU and priority |
| `deployment.yaml`    | The gateway pod: the stock Ray image, with the files above from the `ray-train-gateway` ConfigMap and the workload accounts' declarations from `af-accounts` |
| `service.yaml`       | `ray-train-gateway:10001` for Ray Client and `:8265` for the Jobs API, the addresses notebooks connect to                 |
| `rbac.yaml`          | Create, read and delete RayClusters and RayJobs, and annotate RayJobs; create their token Secrets; create, resume and delete the Jobs that copy data |
| `networkpolicy.yaml` | Admits sessions to the gateway, and the gateway to users' clusters; the namespace's own `np-default` admits its other pods, a workload account's among them, to both |

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

## Jobs on NRP

A submission with the header `af-site: nrp` runs on
[NRP Nautilus](../interlink/nrp) instead, as a RayJob that the gateway
creates there through NRP's API with the interLink plugin's kubeconfig
(`kubeconfig-nrp-interlink`), from [`rayjob-nrp.yaml`](rayjob-nrp.yaml): the
image of the AF's global environment ([`docker/ray-nrp`](../../docker/ray-nrp)),
whose job takes only `af-n-workers` or `af-max-workers`, and all AF jobs there
hold no more than `nrpGpuBudget` GPUs. A RayJob there carries a hash of its
owner, and no name or path of theirs. [`nrp_jobs.py`](nrp_jobs.py) is that NRP
side.

A job anywhere may name directories of the storage its cluster mounts with
`af-inputs` and `af-output`, which it finds in `AF_INPUTS` and `AF_OUTPUT`: at
Purdue the paths themselves, on NRP the job's prefix of the NRP S3 bucket
(`nrp-s3`), `ray-nrp/jobs/<RayJob>/inputs` and `/output`, beside the code a
submission uploads, from which Ray fetches it.

A job on NRP keeps its progress in its RayJob's annotations, and the reaper
moves it on:

1. **inputs** — created suspended, while a Job in `cms`, `<RayJob>-in`, runs
   [`stage.py`](stage.py) as the user to copy its inputs to S3;
2. **placing** — its workers placed, when there is room, on nodes of one region
   and GPU model, as [`nrp_gpus.py`](nrp_gpus.py) reads NRP's Prometheus, with
   its head in that region and `AF_MAX_WORKERS` their number, and resumed;
3. **running** — until it ends, or until it is stopped, which suspends it and
   says why: by its user, for want of room or a cluster within
   `nrpStartTimeoutSeconds`, for inputs that could not be copied, or for GPUs
   busy less than `nrpMinGpuUtilization` percent on average over
   `nrpUtilizationWindowSeconds` of its running, by NRP's GPU metrics, since NRP
   bans a namespace that keeps its GPUs idle;
4. **outputs** — `<RayJob>-out`, made suspended at the submission, copies its
   outputs back to `af-output`; the job reports RUNNING until it has;
5. **done** — its prefix deleted, unless its outputs could not be copied
   back; its RayJob and Jobs, and such a prefix, go `jobRecordSeconds` later.

Its status comes from its RayJob, and its log from the pod that submits it,
which outlasts its cluster: KubeRay removes that as soon as the job ends.

## Tuning

[`config.yaml`](config.yaml) holds the gateway's settings, each with what it
does: the timeouts, the PriorityClass of the preemptible workers, the bounds
of the worker size a call may ask for, and `gpus`,
the kinds of GPU a worker may hold, each with its memory, the most one worker holds, and with the most of
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
kubectl -n cms get rayjobs -l app.kubernetes.io/managed-by=ray-train-gateway
kubectl -n cms get pod -l app=ray-train-gateway
kubectl -n cms logs deploy/ray-train-gateway
kubectl -n cms logs deploy/ray-train-gateway --previous
```

The GPUs free on NRP for workers of the shape `nrpWorker` in `config.yaml`
names, from a session, or from a checkout with `aiohttp` and `pyyaml`:

```bash
curl -s http://ray-train-gateway:8265/af/nrp-gpus
python apps/ray-train/nrp_gpus.py
```
