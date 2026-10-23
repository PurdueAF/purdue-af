# ray-nrp

A Ray Train job on [NRP Nautilus](https://nrp.ai/documentation/), run the way
the AF runs work there: with the facility's credentials, never a user's. A Job
in `cms` writes the training inputs to NRP S3 with Ray Data; a `RayJob` for
NRP's KubeRay operator, submitted as the interlink plugin's service account,
trains on them with PyTorch DDP across two GPU pods and checkpoints to the same
bucket. It is independent of the AF's own Ray Train
([`apps/ray-train`](../../apps/ray-train)) and of the interLink virtual node.

| File                | What it is                                                                         |
| ------------------- | ---------------------------------------------------------------------------------- |
| `prepare.py`        | Writes a synthetic regression set as Parquet shards to `<bucket>/ray-nrp/inputs`; holds the S3 settings both scripts share |
| `train.py`          | The training: `TorchTrainer` over those shards, checkpoints in `<bucket>/ray-nrp/runs` |
| `prepare-job.yaml`  | The Job in `cms` that runs `prepare.py`                                            |
| `rayjob.yaml`       | The `RayJob` in NRP's `interlink-cms`: a CPU head and two worker pods of two GPUs each |

Both scripts reach the pods through the `ray-nrp-test` ConfigMap, the S3
credentials through the `nrp-s3` Secret, under the same names on both
clusters.

## Credentials

`nrp-s3` in `cms` is the facility's NRP S3 Secret, deployed with
[`apps/interlink`](../../apps/interlink/README.md). NRP is reached with `kubeconfig-nrp-interlink`
([`apps/interlink`](../../apps/interlink/README.md#what-is-not-in-git)), whose
Role in [`rbac-interlink-nrp.yaml`](../../apps/interlink/nrp/to-deploy-at-nrp/rbac-interlink-nrp.yaml)
grants RayJobs.

## Running it

From the repository root, with Geddes as the current context. Write the
inputs:

```bash
kubectl -n cms create configmap ray-nrp-test --from-file=workflows/ray-nrp/prepare.py --from-file=workflows/ray-nrp/train.py
kubectl -n cms create -f workflows/ray-nrp/prepare-job.yaml
kubectl -n cms logs -f job/ray-nrp-prepare
```

Act on NRP as the plugin's service account:

```bash
NRP_KUBECONFIG=$(mktemp) && kubectl -n cms get secret kubeconfig-nrp-interlink -o jsonpath='{.data.kubeconfig\.yaml}' | base64 -d >"$NRP_KUBECONFIG"
nrp() { kubectl --kubeconfig "$NRP_KUBECONFIG" -n interlink-cms "$@"; }
```

Copy the S3 credentials and the code to NRP, then submit:

```bash
kubectl -n cms get secret nrp-s3 -o go-template='{{range $k, $v := .data}}{{$k}}={{$v | base64decode}}{{"\n"}}{{end}}' | nrp create secret generic nrp-s3 --from-env-file=/dev/stdin
nrp create configmap ray-nrp-test --from-file=workflows/ray-nrp/prepare.py --from-file=workflows/ray-nrp/train.py
nrp create -f workflows/ray-nrp/rayjob.yaml
```

Follow it:

```bash
nrp get rayjob ray-nrp-test
nrp get pods -o wide -l ray.io/cluster
nrp logs -f -l job-name=ray-nrp-test
```

## What it shows

- **Multi-node DDP**: four Ray Train workers, one per GPU, across both pods
  (`NUM_WORKERS` in `rayjob.yaml`); `-o wide` shows which nodes and sites the
  pods landed on.
- **Recovery**: delete one worker pod mid-run. KubeRay replaces it, Ray Train
  restarts the workers, and the log prints `starting at epoch N` from the last
  checkpoint.
- **Resubmission**: `nrp delete rayjob ray-nrp-test` mid-run, then create it
  again; the new run resumes from the checkpoint in S3 under the same run name.

A change to either script reaches the pods by recreating `ray-nrp-test` on the
cluster that runs it; one to `rayjob.yaml` by deleting and creating the
`RayJob`.

## Cleaning up

```bash
nrp delete rayjob ray-nrp-test
nrp delete configmap ray-nrp-test
nrp delete secret nrp-s3
kubectl -n cms delete configmap ray-nrp-test
rm "$NRP_KUBECONFIG"
```

The finished prepare Job and `RayJob` remove themselves after
`ttlSecondsAfterFinished`. `<bucket>/ray-nrp/` stays in S3 until deleted there.
