# ray-train

A shared Ray cluster for [Ray Train](https://docs.ray.io/en/latest/train/train.html):
AF users submit PyTorch training scripts to its Jobs API from their sessions,
and the training workers run on T4 GPU pods that the Ray autoscaler starts for
the job and removes once idle. How users run a job:
[Training on GPUs with Ray Train](../../docs/docs/guide-ray-train.md).

| File                 | What it is                                                                                                                                    |
| -------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `helmrelease.yaml`   | KubeRay's `ray-cluster` chart, applied after the operator; a post-renderer sets `upgradeStrategy: Recreate`, which the chart has no value for |
| `values.yaml`        | The cluster: image, head, the `gpu` worker group, placement and mounts                                                                        |
| `networkpolicy.yaml` | Admits AF sessions to the Jobs API and nothing else from outside the cluster's own pods                                                       |

The `ray.io` CRDs and their controller are the KubeRay operator in
[`apps/ray/operator`](../ray/operator), and the chart comes from the `kuberay`
HelmRepository in [`apps/ray/helmrepo.yaml`](../ray/helmrepo.yaml).

- **Head**: always up, with no GPU, and advertising no CPUs to Ray. It holds
  the GCS, the dashboard and Jobs API
  (`ray-train-head-svc.cms.svc.cluster.local:8265`), the autoscaler, and every
  job's driver and Train controller.
- **Workers** (`gpu`): one T4 each, between none and `maxReplicas`. Every
  pending Train worker is a request the autoscaler adds a pod for; a pod idle
  for `idleTimeoutSeconds` is removed.
- **Software**: the stock Ray image; each job brings its packages in its
  `runtime_env`.
- **Storage**: `/work`, `/depot/cms`, `/eos` and `/cvmfs` read-only on every
  pod, and `/work/projects/ray-train` read-write. The head's `results-dir` init
  container creates that directory for the image's `ray` user (1000:100).
  Nothing prunes it.

## Tuning

All in `values.yaml`:

| To change                      | Edit                                                                                                                            |
| ------------------------------ | ------------------------------------------------------------------------------------------------------------------------------- |
| the most GPU pods at once      | `worker.maxReplicas`                                                                                                            |
| how long an idle GPU pod stays | `head.autoscalerOptions.idleTimeoutSeconds`                                                                                     |
| the Ray version                | `image.tag`, and the `results-dir` image with it                                                                                |
| the GPU type                   | `worker.resources`; a MIG device also needs `worker.rayStartParams.num-gpus`, since the autoscaler counts only `*gpu` resources |

A change to the cluster's spec recreates all of its pods, which stops the jobs
running on it.

## Checking on it

```bash
kubectl -n cms get raycluster ray-train
kubectl -n cms get pods -l ray.io/cluster=ray-train
```
