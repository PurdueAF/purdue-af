# interLink — virtual nodes

Each subdirectory registers one [interLink](https://github.com/interlink-hq/interLink)
virtual node in the `cms` namespace. A pod scheduled onto such a node is
translated by the node's plugin into work on a remote system — a Slurm job, or
a pod in a remote Kubernetes cluster — and run there; to Kubernetes it still
looks like an ordinary pod.

| Node               | Remote system                  | Plugin                       | Credential (out of band)         | Offloaded work                     | Deployed |
| ------------------ | ------------------------------ | ---------------------------- | -------------------------------- | ---------------------------------- | -------- |
| `interlink-hammer` | Hammer, partition `hammer-nodes` | Slurm sidecar → `sbatch`   | `munge-key-hammer` PVC           | Slurm jobs under Singularity       | yes      |
| `interlink-nrp`    | NRP Nautilus                   | Kubernetes → remote pods     | `kubeconfig-nrp-interlink` Secret | pods in namespace `interlink-cms` | yes      |

Every node is three containers in one Deployment (`<nodeName>-node`) from the
shared chart (`helmrepo.yaml`): the interLink API, the plugin that submits the
work to the remote system, and the virtual kubelet that registers the Node
object. Chart resources are all named `<nodeName>-*`.

hammer's plugin runs the site Slurm sidecar image
([`docker/interlink-slurm-plugin/README.md`](../../docker/interlink-slurm-plugin/README.md));
nrp's runs the site [interlink-kubernetes-plugin](https://github.com/interlink-hq/interlink-kubernetes-plugin)
image ([`docker/interlink-kubernetes-plugin/README.md`](../../docker/interlink-kubernetes-plugin/README.md)).
Both are tagged with their upstream ref (`PLUGIN_REF`), pinned in each node's
values; the k8s image carries the `ensure-namespace` patch, and nrp sets
`ensure_namespace=False` because NRP grants namespaced RBAC only — the
offloading namespace is created out of band.

A node is deployed only once its credential exists — without it the node pod
never starts — so a new node stays commented out in
`flux/experimental/kustomization.yaml` until its credential is created.

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
(`slurm-job.vk.io/flags`, `slurm-job.vk.io/singularity-options`). On
interlink-nrp, GPU pods request `nvidia.com/gpu`, which the node advertises;
NRP places them on any free GPU.

On interlink-nrp only `emptyDir`, ConfigMap, Secret and PersistentVolumeClaim
volumes reach the remote pod; every other volume and its mounts are dropped. A
PVC is offloaded only when the pod names it in the `interlink.io/remote-pvc`
annotation (comma-separated); the plugin uses the claim of that name in
`interlink-cms`. For `/cvmfs`, that claim is `cvmfs`, created out of band
([`nrp/to-deploy-at-nrp/cvmfs-pvc.yaml`](nrp/to-deploy-at-nrp/cvmfs-pvc.yaml))
on NRP's `cvmfs` storage class, so a pod that mounts the AF's `cvmfs` claim
gets the same mount on NRP:

```yaml
metadata:
  annotations:
    interlink.io/remote-pvc: cvmfs
```

Offloading to NRP is batch-style: a service inside an offloaded pod is not
reachable from Geddes. The Kubernetes plugin runs no tunnel client, so
hammer's wstunnel shadow would never connect; making one reachable takes
interLink full mesh, which is not part of this setup.

## What is not in git

The credential each plugin authenticates to its remote system with — created
and populated out of band, never in this repo.

Munge keys live in per-cluster RWX PVCs, never as Secrets.
`munge-key-hammer` is also mounted by the AF sessions
(`apps/jupyterhub/jupyterhub/values.yaml`).
To add a further Slurm cluster: create its PVC, copy that cluster's key in,
then uncomment its entry in the kustomization.

The NRP kubeconfig is the `kubeconfig-nrp-interlink` Secret in `cms`, minted
by [`nrp/to-deploy-at-nrp/generate-nrp-kubeconfig.sh`](nrp/to-deploy-at-nrp/generate-nrp-kubeconfig.sh)
against the RBAC in the same directory, applied to NRP out of band. Its token
is time-bounded: rerun the script and re-create the Secret before it expires.

## Verifying a node

```bash
kubectl get node <nodeName>
```

Schedule a short-lived pod with the nodeSelector/tolerations above — on
hammer, with the Slurm annotations your partition expects. The pod should
reach `Running`: `squeue` on Hammer shows the matching job, and NRP shows the
offloaded pod in `interlink-cms`. Delete it afterwards — it is not managed by
Flux.
