# Ray on the Analysis Facility

The KubeRay operator: the controller that reconciles every `RayCluster` in
`cms`, and the `ray.io` CRDs, cluster-scoped, which the release installs and
upgrades with its chart. Its one consumer is
[`apps/ray-train`](../ray-train/README.md), which creates a Ray cluster per
user.

| path            | what it is                                                                                                                                                                      |
| --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `helmrepo.yaml` | `HelmRepository` for the KubeRay charts                                                                                                                                         |
| `operator/`     | `kuberay-operator` with `singleNamespaceInstall`: it watches `cms` only, under a Role rather than a ClusterRole. `values.yaml` sets only what differs from the chart's defaults |

[`tests/manifests/test_ray.py`](../../tests/manifests/test_ray.py) checks the
Flux wiring and the namespaced install.
