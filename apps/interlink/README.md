# interLink — virtual nodes backed by Slurm

Each subdirectory registers one [interLink](https://github.com/interlink-hq/interLink)
virtual node in the `cms` namespace. A pod scheduled onto such a node is
translated into a Slurm job on the named cluster and run under Singularity;
to Kubernetes it still looks like an ordinary pod.

| Node                 | Cluster  | Slurm partition | Depot path                                | Munge key PVC        | `SLURM_CLUSTER` | Deployed |
| -------------------- | -------- | --------------- | ----------------------------------------- | -------------------- | --------------- | -------- |
| `interlink-hammer`   | Hammer   | `hammer-nodes`  | `/depot/cms/purdue-af/interlink`          | `munge-key-hammer`   | `hammer`        | yes      |
| `interlink-gautschi` | Gautschi | `cpu`           | `/depot/cms/purdue-af/interlink/gautschi` | `munge-key-gautschi` | `gautschi`      | yes      |
| `interlink-negishi`  | Negishi  | —               | `/depot/itap/interlink/negishi`           | `munge-key-negishi`  | `negishi`       | yes      |

All three nodes share one plugin image tagged with the upstream plugin
ref (`PLUGIN_REF`, e.g. `…/interlink-slurm-plugin:0.6.2-pre3`). Cluster
identity comes from `SLURM_CLUSTER` plus the per-cluster munge PVC — never
from a floating `:latest`. CI builds on ghcr; see
[`docker/interlink-slurm-plugin/README.md`](../../docker/interlink-slurm-plugin/README.md)
and [`slurm/README.md`](../../slurm/README.md).

A node is deployed only once its munge key PVC exists — without the key the
node pod never gets past Pending, so a new cluster stays commented out in
`deploy/experimental/kustomization.yaml` until its PVC is populated.

Negishi keeps its own Depot tree (`/depot/itap`) and its custom wstunnel
template, which adds an ingress rule per exposed port; Hammer and Gautschi use
the chart's built-in template.

Each node is three containers in one Deployment (`<nodeName>-node`): the
interLink API, the Slurm sidecar plugin that shells out to `sbatch`, and the
virtual kubelet that registers the Node object. Chart resources are all named
`<nodeName>-*`, so `nodeName` is the one value that must never drift from the
HelmRelease `postRenderers` target.

To use a node, target it explicitly — it carries a
`virtual-node.interlink/no-schedule` taint so nothing lands there by accident:

```yaml
nodeSelector:
  kubernetes.io/hostname: interlink-hammer
tolerations:
  - key: virtual-node.interlink/no-schedule
    operator: Exists
```

Slurm submission options come from pod annotations
(`slurm-job.vk.io/flags`, `slurm-job.vk.io/singularity-options`).

## What is not in git

The munge key each plugin authenticates to Slurm with. Munge keys live in
per-cluster RWX PVCs created and populated out of band — never as Secrets, and
never in this repo.

All three PVCs exist. `munge-key-hammer` and `munge-key-gautschi` are the same
ones the AF sessions (`apps/jupyterhub/jupyterhub/values.yaml`) and the
dask-gateway Slurm gateways mount. To add a further cluster: create its PVC,
copy that cluster's key in, then uncomment its entry in the kustomization.

## Verifying a node

```bash
kubectl get node interlink-hammer
```

Schedule a short-lived pod with the nodeSelector/tolerations above and the
Slurm annotations your partition expects. The pod should reach `Running`;
`squeue` on the cluster shows the matching job. Delete it afterwards — it is
not managed by Flux.
