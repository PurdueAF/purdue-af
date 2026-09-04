# Ray on the Analysis Facility

The production SuperSONIC serving stack, run on Ray: every worker pod runs the
**same Triton** — same image, same arguments, same resources, same model
repository as `apps/sonic/supersonic` — with Ray doing what Envoy and KEDA do
there.

Feature parity is not approximated here. It is the same server, so every
backend (ONNX, TensorRT, PyTorch, TensorFlow, Python, ensembles), every
endpoint, gRPC and HTTP alike, and the binary tensor extension all work exactly
as they do in the supersonic release.

| path | what it is |
| --- | --- |
| `helmrepo.yaml` | `HelmRepository` for the KubeRay charts |
| `operator/` | `kuberay-operator` 1.7.0 — the `ray.io` CRDs and the controller. Namespaced (`singleNamespaceInstall: true`), so both the watch and the RBAC stay in `cms`. |
| `sonic-ray/rayservice.yaml` | the `RayService`: the Ray cluster, the Triton container in every worker pod, and the Serve app that pairs them |
| `sonic-ray/serve/sonic_serve.py` | the Serve replica — a transparent HTTP proxy to the Triton beside it |
| `sonic-ray/triton-service.yaml` | the inference entry point (gRPC + HTTP) on the private pool |
| `sonic-ray/metrics-services.yaml` | what the AF Prometheus scrapes |

## Shape

```
                  sonic-ray-triton (LoadBalancer, private pool)
                     │  gRPC :8001, HTTP :8000
                     ▼
  ┌──────────────────────────────────────────┐   × 1…10 pods
  │ worker pod                               │
  │   triton      1 GPU, 16 CPU, 16G, /models│
  │   ray-worker  Serve replica ──loopback──▶│
  └──────────────────────────────────────────┘
                     ▲
                     │  HTTP :8000 (Serve)
              sonic-ray-head-svc (LoadBalancer, private pool)
```

A `RayService` rather than the upstream `ray-cluster` chart: that chart renders
a bare `RayCluster` with no Serve application, and it is the Serve application
that ties replica count to Triton count.

**One replica, one Triton.** Each worker advertises a custom Ray resource
`triton: 1` and the Serve replica claims one, so a replica can only land on a
pod that has a Triton — and asking for another replica is what makes the Ray
autoscaler add another pod, which brings another GPU and another Triton.

**Two ways in, on purpose.** gRPC goes straight to the Triton containers
through `sonic-ray-triton`, because Ray Serve's gRPC ingress routes on an
`application` metadata key that a stock Triton client never sends — CMSSW's
SONIC clients would not be routable through it. HTTP works there too, but sent
to the Ray head's Serve port it goes through the proxy, which is what lets
Serve's autoscaler see the load. The trade is explicit: **gRPC traffic does not
feed the autoscaler.** Raise `min_replicas` for a gRPC-heavy workload, or scale
the group by hand.

## How it lines up with SuperSONIC

| SuperSONIC (`supersonic`) | Ray (`sonic-ray`) |
| --- | --- |
| Triton: 1 GPU, 16 CPU, 16G, `--model-control-mode=explicit --load-model=*` | the same container, argument for argument |
| Envoy: entry point behind a `LoadBalancer` on `geddes-private-pool`, `ROUND_ROBIN` | `sonic-ray-triton`, same pool, kube-proxy round-robin |
| `ingress.enabled: false` — private pool only | no ingress; the Ray dashboard has no auth and must not be exposed |
| KEDA `ScaledObject`, 1 → 10 Triton replicas | Serve autoscaling 1 → 10, driving the Ray worker autoscaler |
| `nodeSelector: cms-af-prod=true` + the `hub.jupyter.org/dedicated` toleration | same, head and workers |
| model repository PVC at `/models` | same PVC, mounted **read-only** — the model manager owns the writes |
| Triton Service labelled `scrape_metrics: "true"` | `sonic-ray-triton-metrics` (`nv_*`) and `sonic-ray-metrics` (Ray and Serve), same label, `release="sonic-ray"` |

`tests/manifests/test_ray.py` asserts this against `apps/sonic/supersonic/values.yaml`
itself — including a token-by-token comparison of Triton's arguments — so
retuning supersonic and forgetting Ray turns the build red. Those tests also
stand in for kubeconform, which skips `RayService`: `ray.io` has no schema in
the CRDs-catalog.

Two differences remain, both structural:

- **Autoscaling signal.** KEDA scales Triton on a Prometheus expression
  (`serverLoadMetric`, threshold 100); Serve scales on ongoing requests per
  replica, and only sees HTTP that arrives through the proxy. KubeRay 1.7.0
  exposes no `scale` subresource on `RayCluster`, so KEDA cannot drive a worker
  group directly — closing this properly means a controller that patches
  `workerGroupSpecs[].replicas` from the same PromQL.
- **Dashboards.** SuperSONIC ships its own Grafana at
  `supersonic-grafana.geddes.rcac.purdue.edu`. The `nv_*` series land in the AF
  Prometheus tagged `release="sonic-ray"`, ready for the central AF Grafana; no
  second Grafana is deployed here.

## Using it

```bash
kubectl -n cms get svc sonic-ray-triton      # MetalLB address on the private pool
SONIC=<address>

curl -s "http://$SONIC:8000/v2/health/ready" -o /dev/null -w '%{http_code}\n'
curl -s "http://$SONIC:8000/v2/repository/index" | jq
```

CMSSW clients point at `$SONIC:8001`, exactly as they point at the supersonic
release's Envoy address.

The Ray dashboard, for Serve deployment status and replica logs:

```bash
kubectl -n cms port-forward svc/sonic-ray-head-svc 8265:8265
```

## Cost

The worker group idles at one GPU (`minReplicas: 1`) on the same `cms-af-prod`
nodes SuperSONIC and the user sessions compete for. Set the group's
`replicas`/`minReplicas` and the Serve deployment's `min_replicas` to 0 to park
it without uninstalling.
