# Ray on the Analysis Facility

A KubeRay deployment that approximates the production SuperSONIC release
(`apps/sonic/supersonic`) as closely as a Ray cluster can, so the two can be
compared on the same hardware.

Both pieces are thin wrappers over upstream charts from the
[KubeRay Helm repository](https://ray-project.github.io/kuberay-helm/), pinned
to 1.7.0 and configured entirely through a values ConfigMap — the same pattern
`apps/sonic/supersonic` uses for the SuperSONIC chart.

| path | what it is |
| --- | --- |
| `helmrepo.yaml` | `HelmRepository` for the KubeRay charts |
| `operator/` | `kuberay-operator` — installs the `ray.io` CRDs and the controller |
| `sonic-ray/` | `ray-cluster` — the RayCluster itself, plus its metrics Service |

The operator is namespaced (`singleNamespaceInstall: true`), so it watches and
holds RBAC for `cms` only.

## How it lines up with SuperSONIC

| SuperSONIC (`supersonic`) | Ray (`sonic-ray`) |
| --- | --- |
| Triton pods: 1 GPU, 16 CPU, 16G | worker group `gpu-group`, identical requests/limits |
| `nodeSelector: cms-af-prod=true` + `hub.jupyter.org/dedicated=cms-af` toleration | same, on head and workers |
| Envoy: gRPC entry point behind a `LoadBalancer` on `geddes-private-pool` | Ray head Service, `LoadBalancer`, same MetalLB pool |
| `ingress.enabled: false` — private pool only | no ingress; the Ray dashboard has no auth and must not be exposed |
| KEDA `ScaledObject`, 1 → 10 Triton replicas | in-tree Ray autoscaler, `minReplicas: 1`, `maxReplicas: 10` |
| model repository PVC `supersonic-model-repository` at `/models` | same PVC on the workers, mounted **read-only** |
| Triton Service labelled `scrape_metrics: "true"` | `sonic-ray-metrics` Service, same label |

## What it does *not* do

**It serves nothing yet.** The `ray-cluster` chart deploys a bare RayCluster —
a runtime with GPUs attached, not an inference service. There is no Ray Serve
application, so nothing in this release answers requests the way Triton does
behind Envoy; the head's `serve` port (8000) is declared and routable, but
closed until an app is deployed onto the cluster.

Getting to an actual SuperSONIC equivalent means deploying a Serve app that
loads the models under `/models`. Upstream's way to do that declaratively is
the `RayService` CR, which the `ray-cluster` chart does not render — it would
need either a hand-written `RayService` manifest here, or `serve deploy`
against the running cluster.

Two other differences are structural, not oversights:

- **Autoscaling signal.** KEDA scales Triton on a Prometheus expression
  (`serverLoadMetric`, threshold 100). The Ray autoscaler scales on pending
  Ray tasks and actors and has no equivalent knob, so the two react to load
  differently even with the same replica bounds.
- **Dashboards.** The SuperSONIC release ships its own Grafana with an ingress
  at `supersonic-grafana.geddes.rcac.purdue.edu`. Ray metrics land in the AF
  Prometheus (`prometheus-server.cms`) via the metrics Service and can be
  added to the central AF Grafana; no second Grafana is deployed here.

## Reaching the cluster

```bash
kubectl -n cms get svc sonic-ray-head-svc         # MetalLB address, private pool
kubectl -n cms port-forward svc/sonic-ray-head-svc 8265:8265   # dashboard
```

## Cost

The worker group idles at one GPU (`minReplicas: 1`), on the same
`cms-af-prod` nodes SuperSONIC and the user sessions compete for. Set
`worker.replicas`/`worker.minReplicas` to 0 to park it without uninstalling.
