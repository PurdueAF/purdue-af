# interLink — virtual nodes backed by Slurm

Each subdirectory registers one [interLink](https://github.com/interlink-hq/interLink)
virtual node in the `cms` namespace. A pod scheduled onto such a node is
translated into a Slurm job on the named cluster and run under Singularity;
to Kubernetes it still looks like an ordinary pod.

| Node                 | Cluster  | Slurm partition | Depot path                                | Munge key PVC        | Deployed |
| -------------------- | -------- | --------------- | ----------------------------------------- | -------------------- | -------- |
| `interlink-hammer`   | Hammer   | `hammer-nodes`  | `/depot/cms/purdue-af/interlink`          | `munge-key-hammer`   | yes      |
| `interlink-gautschi` | Gautschi | `cpu`           | `/depot/cms/purdue-af/interlink/gautschi` | `munge-key-gautschi` | yes      |
| `interlink-negishi`  | Negishi  | —               | `/depot/itap/interlink/negishi`           | `munge-key-negishi`  | no       |

A node is deployed only once its munge key PVC exists; the others stay
commented out in `deploy/experimental/kustomization.yaml`, since without the
key the node pod never gets past Pending.

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
(`slurm-job.vk.io/flags`, `slurm-job.vk.io/singularity-options`); see
`<cluster>/test-pod.yaml` for a working example.

## What is not in git

The munge key each plugin authenticates to Slurm with. Munge keys live in
per-cluster RWX PVCs created and populated out of band — never as Secrets, and
never in this repo.

`munge-key-hammer` and `munge-key-gautschi` already exist — the same PVCs the
AF sessions (`apps/jupyterhub/jupyterhub/values.yaml`) and the dask-gateway
Slurm gateways mount, so neither node needs anything new. `munge-key-negishi`
does not exist: create it like the others, copy Negishi's key in, then
uncomment Negishi in the kustomization.

## Verifying a node

```bash
kubectl get node interlink-hammer
kubectl apply -n cms -f apps/interlink/hammer/test-pod.yaml
kubectl logs -n cms test-interlink-hammer
```

The pod should reach `Running` and print its hello line; `squeue` on the
cluster shows the matching job. Delete it afterwards — it is not managed by
Flux.
