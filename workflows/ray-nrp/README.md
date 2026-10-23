# ray-nrp

A Ray Train job on [NRP Nautilus](https://nrp.ai/documentation/), run from a
checkout: the inputs written to NRP S3 with Ray Data from an AF session, and a
`RayJob` for NRP's KubeRay operator that trains on them with PyTorch DDP across
two GPU pods, checkpointing to the same bucket. It is independent of the AF's
own Ray Train ([`apps/ray-train`](../../apps/ray-train)) and of the interLink
virtual node; it uses the NRP namespace `interlink-cms` and your own NRP
credentials.

| File          | What it is                                                                         |
| ------------- | ---------------------------------------------------------------------------------- |
| `prepare.py`  | Writes a synthetic regression set as Parquet shards to `<bucket>/ray-nrp/inputs` |
| `train.py`    | The training: `TorchTrainer` over those shards, checkpoints in `<bucket>/ray-nrp/runs` |
| `rayjob.yaml` | The `RayJob`: a CPU head and two worker pods of two GPUs each, `train.py` mounted from a ConfigMap |

## Running it

An S3 bucket and its keys come from the NRP portal
([Ceph S3](https://nrp.ai/documentation/userdocs/storage/ceph-s3/)). Put them in
an env file outside the repository:

```
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_ENDPOINT_URL=https://s3-central.nrp-nautilus.io
S3_BUCKET=...
```

Write the inputs from an AF session, in the global environment:

```bash
set -a; source ~/ray-nrp-s3.env; set +a
python workflows/ray-nrp/prepare.py
```

Create the credentials and the code on NRP, then the job:

```bash
kubectl --context nautilus -n interlink-cms create secret generic ray-nrp-test-s3 --from-env-file="$HOME/ray-nrp-s3.env"
kubectl --context nautilus -n interlink-cms create configmap ray-nrp-test --from-file=workflows/ray-nrp/train.py
kubectl --context nautilus apply -f workflows/ray-nrp/rayjob.yaml
```

Follow it:

```bash
kubectl --context nautilus -n interlink-cms get rayjob ray-nrp-test
kubectl --context nautilus -n interlink-cms get pods -o wide -l ray.io/cluster
kubectl --context nautilus -n interlink-cms logs -f job/ray-nrp-test
```

## What it shows

- **Multi-node DDP**: four Ray Train workers, one per GPU, across both pods
  (`NUM_WORKERS` in `rayjob.yaml`); `-o wide` shows which nodes and sites the
  pods landed on.
- **Recovery**: delete one worker pod mid-run. KubeRay replaces it, Ray Train
  restarts the workers, and the log prints `starting at epoch N` from the last
  checkpoint.
- **Resubmission**: delete the `RayJob` mid-run and apply it again; the new
  run resumes from the checkpoint in S3 under the same run name.

`train.py` changes reach NRP by recreating the ConfigMap; `rayjob.yaml`
changes by deleting and applying the `RayJob`.

## Cleaning up

```bash
kubectl --context nautilus -n interlink-cms delete rayjob ray-nrp-test
kubectl --context nautilus -n interlink-cms delete configmap ray-nrp-test
kubectl --context nautilus -n interlink-cms delete secret ray-nrp-test-s3
```

The finished `RayJob` removes its cluster after `ttlSecondsAfterFinished`.
`<bucket>/ray-nrp/` stays in S3 until deleted there.
