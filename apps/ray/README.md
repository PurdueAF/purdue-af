# Ray on the Analysis Facility

A Ray Serve inference server approximating the production SuperSONIC release
(`apps/sonic/supersonic`) as closely as Ray can, so the two can be compared on
the same hardware — same nodes, same GPU shape, same replica range, same model
repository, same private-pool entry point.

| path | what it is |
| --- | --- |
| `helmrepo.yaml` | `HelmRepository` for the KubeRay charts |
| `operator/` | `kuberay-operator` 1.7.0 — the `ray.io` CRDs and the controller. Namespaced (`singleNamespaceInstall: true`), so both the watch and the RBAC stay in `cms`. |
| `sonic-ray/rayservice.yaml` | the `RayService`: the Ray cluster *and* the Serve application it runs |
| `sonic-ray/serve/sonic_serve.py` | the server itself, mounted into the pods from a ConfigMap |
| `sonic-ray/metrics-service.yaml` | the Service the AF Prometheus scrapes |

A `RayService` rather than the upstream `ray-cluster` chart because that chart
renders a bare `RayCluster`: it has no field for a Serve application, so a
cluster it installs answers no inference requests. RayService owns both, and
rolls a new cluster in on upgrade (`upgradeStrategy: NewCluster`) rather than
mutating the running one.

## What it serves

`sonic_serve.py` speaks the **KServe v2 REST protocol** (the Open Inference
Protocol) — the same protocol Triton serves in the SuperSONIC release — over
the **same Triton model repository**, mounted read-only from
`supersonic-model-repository`:

```
GET  /v2                       server metadata, incl. the active ORT providers
GET  /v2/health/live
GET  /v2/health/ready
GET  /v2/repository/index      what the repository holds, and why anything is not served
GET  /v2/models/{name}         model metadata (inputs/outputs, from the ONNX graph)
GET  /v2/models/{name}/ready
POST /v2/models/{name}/infer   inference
```

The repository layout is Triton's (`<repo>/<model>/<version>/model.onnx`,
`config.pbtxt` optional) but the backend is **ONNX Runtime, not Triton**. Two
consequences, both deliberate:

- **ONNX only.** TensorRT plans, SavedModels and TorchScript are listed by
  `/v2/repository/index` as `UNAVAILABLE` with a reason rather than served.
  Serving those would mean embedding Triton in the Ray replicas, which would
  make the comparison meaningless — the point is Ray Serve's own serving stack.
- **JSON tensors only.** Triton's binary tensor extension is not implemented,
  so large inputs pay JSON encoding. Fine for correctness and for comparing
  scheduling behaviour; not a throughput benchmark.

Models load on first request and stay loaded, so a replica warms up as traffic
reaches it instead of paying for the whole repository at startup.

The Ray image ships CUDA but no inference runtime, so the Serve config's
`runtime_env` pins `onnxruntime-gpu` and the matching `nvidia-cudnn-cu12`
wheel; the app loads those shared objects out of site-packages before creating
a session. If the GPU provider still cannot be built, the replica serves on CPU
and says so — in the replica log and in `GET /v2` — rather than failing to come
up.

## How it lines up with SuperSONIC

| SuperSONIC (`supersonic`) | Ray (`sonic-ray`) |
| --- | --- |
| Triton pods: 1 GPU, 16 CPU, 16G | Serve replicas on `gpu-group` workers, identical requests/limits, one GPU each |
| Envoy: entry point behind a `LoadBalancer` on `geddes-private-pool` | Ray head running the Serve HTTP proxy, same MetalLB pool, port 8000 |
| `ingress.enabled: false` — private pool only | no ingress; the Ray dashboard has no auth and must not be exposed |
| KEDA `ScaledObject`, 1 → 10 Triton replicas | Serve autoscaling 1 → 10, which drives the Ray worker autoscaler to add pods |
| `nodeSelector: cms-af-prod=true` + the `hub.jupyter.org/dedicated` toleration | same, on head and workers |
| model repository PVC at `/models` | same PVC, mounted **read-only** |
| Triton Service labelled `scrape_metrics: "true"` | `sonic-ray-metrics`, same label; Ray and Ray Serve metrics both land there |
| KServe v2 over HTTP **and gRPC** | KServe v2 over HTTP |

`tests/manifests/test_ray.py` asserts the mirroring against
`apps/sonic/supersonic/values.yaml` itself, so retuning supersonic and
forgetting Ray turns the build red. Those tests also stand in for kubeconform,
which skips `RayService` — `ray.io` has no schema in the CRDs-catalog.

Two differences are structural rather than gaps:

- **Autoscaling signal.** KEDA scales Triton on a Prometheus expression
  (`serverLoadMetric`, threshold 100). Serve scales on ongoing requests per
  replica, so the two react to the same load differently even with identical
  bounds.
- **Dashboards.** SuperSONIC ships its own Grafana at
  `supersonic-grafana.geddes.rcac.purdue.edu`. Ray's metrics land in the AF
  Prometheus (`prometheus-server.cms`) and can be added to the central AF
  Grafana; no second Grafana is deployed here.

## Using it

```bash
kubectl -n cms get svc sonic-ray-head-svc     # MetalLB address on the private pool
RAY=<address>

curl -s "http://$RAY:8000/v2/health/ready"
curl -s "http://$RAY:8000/v2/repository/index" | jq
curl -s "http://$RAY:8000/v2/models/<model>" | jq

curl -s -X POST "http://$RAY:8000/v2/models/<model>/infer" \
  -H 'Content-Type: application/json' \
  -d '{"inputs":[{"name":"input","datatype":"FP32","shape":[1,4],"data":[1,2,3,4]}]}'
```

The Ray dashboard, which shows Serve deployment status and replica logs:

```bash
kubectl -n cms port-forward svc/sonic-ray-head-svc 8265:8265
```

## Cost

The worker group idles at one GPU (`minReplicas: 1`), on the same
`cms-af-prod` nodes SuperSONIC and the user sessions compete for. Set the
group's `replicas`/`minReplicas` and the Serve deployment's `min_replicas` to 0
to park it without uninstalling.
