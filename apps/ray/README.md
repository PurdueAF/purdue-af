# Ray on the Analysis Facility

The production SuperSONIC serving stack, run on Ray: every worker pod runs the
**same Triton** — same image, same arguments, same resources, same model
repository as `apps/sonic/supersonic` — with KubeRay scheduling the pods and
the Ray autoscaler sizing the group.

Feature parity is not approximated. It is the same server, so every backend
(ONNX, TensorRT, PyTorch, TensorFlow, Python, ensembles), every endpoint, gRPC
and HTTP alike, and the binary tensor extension all work exactly as they do in
the supersonic release.

Autoscaling deliberately does **not** copy SuperSONIC. There is no KEDA here
and no Prometheus in the loop — it is Ray's autoscaler, asked for capacity by a
controller that reads Triton's own queue depth.

| path | what it is |
| --- | --- |
| `helmrepo.yaml` | `HelmRepository` for the KubeRay charts |
| `operator/` | `kuberay-operator` 1.7.0 — the `ray.io` CRDs and the controller. Namespaced (`singleNamespaceInstall: true`), so both the watch and the RBAC stay in `cms`. |
| `sonic-ray/rayservice.yaml` | the `RayService`: the Ray cluster, the Triton container in every worker pod, and the autoscaling controller |
| `sonic-ray/serve/sonic_serve.py` | the controller — turns Triton queue depth into Ray resource demand |
| `sonic-ray/triton-service.yaml` | the inference entry point (gRPC + HTTP) on the private pool |
| `sonic-ray/metrics-services.yaml` | what the AF Prometheus scrapes |

## Shape

```
     clients ──▶ sonic-ray-triton (LoadBalancer, private pool)
                    │  gRPC :8001, HTTP :8000
                    ▼
   ┌─────────────────────────────────────────────┐   × 1…10 pods
   │ worker pod                                  │
   │   triton      1 GPU, 16 CPU, 16G, /models   │
   │   ray-worker  raylet, advertises triton: 1  │
   └─────────────────────────────────────────────┘
          ▲ scrapes :8002        │ request_resources()
          │                      ▼
   ┌─────────────────────────────────────────────┐
   │ head pod: TritonAutoscaler + Ray autoscaler │
   └─────────────────────────────────────────────┘
```

Nothing proxies inference. Clients reach Triton directly through
`sonic-ray-triton`, the way they reach Envoy in the supersonic release —
one address, kube-proxy doing the round-robin.

A `RayService` rather than the upstream `ray-cluster` chart: that chart renders
a bare `RayCluster`, with nowhere to declare the controller.

## Autoscaling

Because inference never touches a Ray task or actor, Ray sees no demand and
would leave the group at its floor forever. `TritonAutoscaler` — one replica,
pinned to the head, holding no GPU — closes that loop every 10 seconds:

```
pending  = Σ nv_inference_pending_request_count over every live Triton
desired  = clamp(ceil(pending / target_pending_per_server), min, max)
request_resources(bundles=[{"triton": 1}] * desired)
```

`request_resources` is the Ray autoscaler's public "make room for this" API.
Each worker advertises `triton: 1`, so a bundle *is* a GPU pod with a Triton in
it, and the autoscaler adds and removes pods to match. **Nothing else in the
cluster asks for that resource** — no Serve replicas on workers, no proxies
(`proxy_location: HeadOnly`) — which is what leaves an unwanted worker idle and
therefore reclaimable.

Scaling up is immediate. Scaling down waits for `downscale_delay_s` of
sustained low demand, because releasing a pod kills the Triton in it; the pod
then gets `terminationGracePeriodSeconds: 90` against Triton's
`--exit-timeout-secs=60` to drain in-flight requests.

The policy lives in the deployment's `user_config`, so retuning it is a config
change rather than a restart:

| key | default | meaning |
| --- | --- | --- |
| `min_servers` / `max_servers` | 1 / 10 | bounds, inside the worker group's own `minReplicas`/`maxReplicas` |
| `target_pending_per_server` | 16 | queued-or-executing requests per Triton before another is asked for |
| `control_interval_s` | 10 | how often the decision is made |
| `downscale_delay_s` | 300 | sustained low demand required before giving a pod back |

`GET /` on the head's Serve port returns the last decision and what it was
based on:

```bash
kubectl -n cms port-forward svc/sonic-ray-head-svc 8000:8000
curl -s localhost:8000 | jq
```

## How it lines up with SuperSONIC

| SuperSONIC (`supersonic`) | Ray (`sonic-ray`) |
| --- | --- |
| Triton: 1 GPU, 16 CPU, 16G, `--model-control-mode=explicit --load-model=*` | the same container, argument for argument |
| Envoy: entry point behind a `LoadBalancer` on `geddes-private-pool`, `ROUND_ROBIN` | `sonic-ray-triton`, same pool, kube-proxy round-robin |
| `ingress.enabled: false` — private pool only | no ingress; the head is `ClusterIP`, dashboard by port-forward only |
| KEDA `ScaledObject` on a Prometheus expression | Ray autoscaler, driven from Triton's own gauge — see above |
| `nodeSelector: cms-af-prod=true` + the `hub.jupyter.org/dedicated` toleration | same, head and workers |
| model repository PVC at `/models` | same PVC, mounted **read-only** — the model manager owns the writes |
| Triton Service labelled `scrape_metrics: "true"` | `sonic-ray-triton-metrics` (`nv_*`) and `sonic-ray-metrics` (Ray), same label, `release="sonic-ray"` |

`tests/manifests/test_ray.py` asserts the Triton parity against
`apps/sonic/supersonic/values.yaml` itself — including a token-by-token
comparison of Triton's arguments — so retuning supersonic and forgetting Ray
turns the build red. It also asserts the properties the scaling loop depends
on: the controller stays off the workers, the workers stay free of Ray work,
and no KEDA resource creeps back in. Those tests stand in for kubeconform,
which skips `RayService`: `ray.io` has no schema in the CRDs-catalog.

The remaining difference is **dashboards**: SuperSONIC ships its own Grafana at
`supersonic-grafana.geddes.rcac.purdue.edu`, while the `nv_*` series here land
in the AF Prometheus tagged `release="sonic-ray"`, ready for the central AF
Grafana.

## Using it

```bash
kubectl -n cms get svc sonic-ray-triton      # MetalLB address on the private pool
SONIC=<address>

curl -s "http://$SONIC:8000/v2/health/ready" -o /dev/null -w '%{http_code}\n'
curl -s "http://$SONIC:8000/v2/repository/index" | jq
```

CMSSW clients point at `$SONIC:8001`, exactly as they point at the supersonic
release's Envoy address.

The Ray dashboard, for cluster and autoscaler state:

```bash
kubectl -n cms port-forward svc/sonic-ray-head-svc 8265:8265
```

## Cost

The worker group idles at one GPU (`minReplicas: 1` and `min_servers: 1`) on
the same `cms-af-prod` nodes SuperSONIC and the user sessions compete for. An
upgrade costs one more for its duration (`upgradeStrategy: NewCluster` brings a
second cluster up before cutting over; if no GPU is free it waits and the old
one keeps serving). Set both floors to 0 to park the release without
uninstalling it.
