# Ray on the Analysis Facility

SuperSONIC's **Triton**, run on Ray: every worker pod carries the same Triton
as `apps/sonic/supersonic` — same image, arguments, resources, model
repository — and **Ray Serve's gRPC proxy carries Triton's protocol** to it.
Serve speaks that protocol because it is handed Triton's own generated
servicer; the only code of ours is a forwarder that passes each RPC from the
proxy to the Triton in its pod. Serve counts every request on the way through,
sizes the deployment from that, and the Ray autoscaler adds a GPU pod for each
replica with nowhere to go.

No custom image, no protocol code, no model code: official Ray, official
Triton, ~100 lines of glue shipped as a ConfigMap, Triton's Python stubs
pip-installed by an init container.

| path | what it is |
| --- | --- |
| `helmrepo.yaml` | `HelmRepository` for the KubeRay charts |
| `operator/` | `kuberay-operator` 1.7.0 — the `ray.io` CRDs and the controller. Namespaced (`singleNamespaceInstall: true`), so both the watch and the RBAC stay in `cms`. |
| `sonic-ray/chart/` | the `sonic-ray` chart: a `RayService` with a Triton in every worker pod and the forwarder as its Serve application, the ConfigMap carrying the forwarder, two metrics Services |
| `sonic-ray/chart/files/sonic_ray/serve_app.py` | the forwarder — one replica per pod, every unary RPC of `GRPCInferenceService` handed to the pod's Triton unchanged |
| `sonic-ray/helmrelease.yaml`, `sonic-ray/values.yaml` | the AF release: `dependsOn` the operator, values with supersonic's `triton:` block |
| [`tests/sonic_ray/`](../../tests/sonic_ray), [`tests/manifests/test_ray.py`](../../tests/manifests/test_ray.py) | source-level checks of the forwarder; rendered-chart checks incl. parity with `apps/sonic/supersonic/values.yaml` |

The chart lives here (like `apps/sonic/model-manager`) rather than being a raw
`RayService` because of ordering: until the operator's chart has installed the
`ray.io` CRDs that is an unknown kind, and kustomize-controller aborts an apply
on the first one it meets — on a fresh cluster, before the HelmRelease that
would install them. `dependsOn: kuberay-operator` is the fix, and a
`HelmRelease` is the only object that can carry it.

## Shape

```
     clients ──▶ sonic-ray-serve (LoadBalancer, private pool) :8001
                    │  Triton gRPC: ModelInfer, ModelMetadata, …
                    ▼
     Serve gRPC proxy (Triton's servicer) on head and every worker
                    │  counted, balanced, autoscaled by Serve
                    ▼
   ┌─────────────────────────────────────────────────┐   × 1…4 pods
   │ worker pod                                      │
   │   ray-worker  raylet advertising triton: 1,     │
   │               proxy, TritonProxy replica ──┐    │
   │   triton      1 GPU, 16 CPU, 16G, /models ◀┘    │  localhost:8001
   └─────────────────────────────────────────────────┘
                    ▲ replica demand
   ┌─────────────────────────────────────────────────┐
   │ head pod: Serve controller, Ray autoscaler      │  (0 CPUs for work, no GPU)
   └─────────────────────────────────────────────────┘
```

A **pod is one Triton on one GPU**, the unit SuperSONIC scales by too. A
**replica is one pod**: every worker advertises one `triton` resource and
every replica claims one, so a replica lands next to its Triton and nowhere
else. Nothing else claims the resource, which is what leaves a pod without a
replica idle and therefore reclaimable, and a replica without a pod pending —
the request that grows the group.

## What it serves and speaks

Everything the `supersonic` release does, because it is the same server on
the same repository (`supersonic-model-repository`, read-only, written by the
[model manager](../sonic/model-manager)): all ten CMS models, every backend,
`config.pbtxt` semantics, dynamic batching, the repository index.

The wire protocol is Triton's gRPC. `GET`-style HTTP is **not** carried
(Serve's HTTP proxy on 8000 answers only its own `/-/healthz` and
`/-/routes`); Triton's HTTP port stays inside the pod. CMSSW's `TritonClient`
speaks gRPC, so `cmsRun` jobs point at `sonic-ray-serve:8001` exactly as they
point at supersonic's Envoy — the port is Triton's conventional one on purpose.
`tritonclient.grpc` works the same way.

The one RPC not forwarded is `ModelStreamInfer`, Triton's bidirectional
stream: Serve's proxy carries unary and server-streaming calls only. CMSSW
uses the unary `ModelInfer`.

## Autoscaling

Two loops, both Ray's, nothing else in between:

1. **Ray Serve** sizes the deployment from the requests its gRPC proxy
   forwards. When the average number in flight per replica exceeds
   `serve.targetOngoingRequests` (16) for `upscaleDelayS` (10 s) it adds a
   replica; when it falls well below for `downscaleDelayS` (300 s) it removes
   one, giving in-flight requests `gracefulShutdownTimeoutS` (60 s). Bounds are
   `serve.minReplicas`/`maxReplicas` (1/4 on the AF).
2. **The Ray autoscaler** sizes the cluster. A new replica needs a `triton`
   resource; if no worker has one free, that is a pending request and the
   autoscaler adds a pod to `gpu-group` (bounded by `ray.worker.minReplicas`/
   `maxReplicas`, 0/4). A worker whose replica is gone idles for
   `idleTimeoutSeconds` (60 s) and is reclaimed; the pod then gets
   `terminationGracePeriodSeconds` against Triton's `--exit-timeout-secs` to
   drain (the chart refuses to render if the first is not larger).

The chart ties the two together: `serve.maxReplicas` may not exceed
`ray.worker.maxReplicas`. Raising the GPU ceiling is one edit in
`sonic-ray/values.yaml`:

```yaml
ray: { worker: { maxReplicas: 8 } }
serve: { maxReplicas: 8 }
```

A replica only becomes ready once its Triton answers `ServerReady`, and it
polls `ServerLive` as its health check, so Serve never routes to a pod whose
Triton is still loading or has died — Serve restarts the replica, and Ray
reclaims a pod that stays broken.

## How it lines up with SuperSONIC

| SuperSONIC (`supersonic`) | Ray (`sonic-ray`) |
| --- | --- |
| Triton: 1 GPU, 16 CPU, 16G, `--model-control-mode=explicit --load-model=*` | the same container, argument for argument |
| Envoy: gRPC entry point behind a `LoadBalancer` on `geddes-private-pool`, `ROUND_ROBIN` | Serve's gRPC proxy behind KubeRay's serve Service, same pool, port 8001 |
| `ingress.enabled: false` — private pool only | no ingress; the head is `ClusterIP`, dashboard by port-forward only |
| KEDA `ScaledObject` on a Prometheus expression, 1–10 pods | Ray Serve request-based autoscaling, 1–4 pods — see above |
| `nodeSelector: cms-af-prod=true` + the `hub.jupyter.org/dedicated` toleration | same, head and workers |
| model repository PVC at `/models` | same PVC, mounted **read-only** — the model manager owns the writes |
| Triton Service labelled `scrape_metrics: "true"` | `sonic-ray-triton-metrics` (`nv_*`) and `sonic-ray-metrics` (Ray, incl. `ray_serve_*`), same label, `release="sonic-ray"` |
| Envoy's Lua rate limiter on `RepositoryIndex` | none; Serve's `maxOngoingRequests` back-pressure instead |

`tests/manifests/test_ray.py` asserts the parity against
`apps/sonic/supersonic/values.yaml` itself — including a token-by-token
comparison of Triton's arguments — so retuning supersonic and forgetting Ray
turns the build red.

## Using it

```bash
kubectl -n cms get svc sonic-ray-serve            # MetalLB address on the private pool
SONIC=<address>:8001
```

CMSSW clients point at `$SONIC`, exactly as they point at the supersonic
release's Envoy address. From Python:

```python
import tritonclient.grpc as grpcclient
client = grpcclient.InferenceServerClient("<address>:8001")
client.is_server_ready()
client.get_model_repository_index()
```

The Ray dashboard, for Serve and autoscaler state:

```bash
kubectl -n cms port-forward svc/sonic-ray-head-svc 8265:8265
```

## What is not an image

The Ray containers run `rayproject/ray:2.52.0-py312-cpu` (through the geddes
Docker Hub proxy cache) exactly as published; the Triton container runs
`nvcr.io/nvidia/tritonserver:26.04-py3`, the tag supersonic runs. Two things
are added at deploy time instead of build time:

- **the forwarder** — `files/sonic_ray/*.py` become the `sonic-ray-code`
  ConfigMap, mounted at `/serve_app/sonic_ray` on head and workers. Its hash
  is annotated onto both pod templates, so a code change rolls the cluster.
- **Triton's Python stubs** — `python.pip` (`tritonclient==2.48.0`, the last
  release whose generated stubs match the protobuf 4 in the Ray image, plus
  `python-rapidjson`) is pip-installed `--no-deps --target` into an emptyDir
  by an init container on every pod, and that directory is on `PYTHONPATH`.
  Serve's proxies import the servicer from it at startup, on every node,
  which is why a `runtime_env` (replicas only) would not do.

The price is a small pip download per pod start and a dependency on PyPI
being reachable from the nodes — chosen over maintaining an image.

## Cost

One GPU idles (`serve.minReplicas: 1`) on the same `cms-af-prod` nodes
SuperSONIC and the user sessions compete for. An upgrade costs a second set
for its duration: `upgradeStrategy: NewCluster` brings a second cluster up
before cutting over, and if no GPU is free it waits while the old one keeps
serving. A GPU node here has 128 cores, so the two extra CPUs the Ray
container adds to each pod change nothing about what fits.
