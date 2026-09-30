# Ray on the Analysis Facility

The KubeRay operator: the `ray.io` CRDs and the controller that reconciles
every `RayCluster` in `cms`. Its one consumer is
[`apps/ray-train`](../ray-train/README.md), which creates a Ray cluster per
user.

| path            | what it is                                                                                                                                              |
| --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `helmrepo.yaml` | `HelmRepository` for the KubeRay charts                                                                                                                 |
| `operator/`     | `kuberay-operator` — the `ray.io` CRDs and the controller, installed namespaced (`singleNamespaceInstall`), so both the watch and the RBAC stay in `cms` |

[`tests/manifests/test_ray.py`](../../tests/manifests/test_ray.py) checks the
Flux wiring and the namespaced install.
