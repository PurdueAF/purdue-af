# Ray on the Analysis Facility

**Triton on Ray**: every worker pod carries NVIDIA's Triton Inference Server,
and **Ray Serve's gRPC proxy carries Triton's protocol** to it. Serve is
handed Triton's own generated servicer; the only code of ours is a forwarder
that passes each RPC from the proxy to the Triton in its pod. Serve counts
every request on the way through, sizes the deployment from that, and the Ray
autoscaler adds a GPU pod for each replica with nowhere to go. The Ray and
Triton containers run published images unchanged.

| path                                                                                                            | what it is                                                                                                                                                                     |
| --------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `helmrepo.yaml`                                                                                                 | `HelmRepository` for the KubeRay charts                                                                                                                                        |
| `operator/`                                                                                                     | `kuberay-operator` — the `ray.io` CRDs and the controller, installed namespaced (`singleNamespaceInstall`), so both the watch and the RBAC stay in `cms`                       |
| [`helm/sonic-ray/`](../../helm/sonic-ray)                                                                       | the `sonic-ray` chart: a `RayService` with a Triton in every worker pod and the forwarder as its Serve application, the ConfigMap carrying the forwarder, two metrics Services |
| [`helm/sonic-ray/files/sonic_ray/serve_app.py`](../../helm/sonic-ray/files/sonic_ray/serve_app.py)              | the forwarder — one replica per pod, every unary RPC of `GRPCInferenceService` handed to the pod's Triton unchanged                                                            |
| `sonic-ray/helmrelease.yaml`, `sonic-ray/values.yaml`                                                           | the AF release: `dependsOn` the operator, the Triton image, arguments, resources and probes, and the CVMFS model repositories                                                  |
| [`tests/sonic_ray/`](../../tests/sonic_ray), [`tests/manifests/test_ray.py`](../../tests/manifests/test_ray.py) | source-level checks of the forwarder; rendered-chart checks of the pod, its ports, its probes and the scaling bounds                                                           |

The chart's conventions: [`helm/README.md`](../../helm/README.md).

## Shape

```
     clients ──▶ sonic-ray-serve (LoadBalancer, private pool)
                    │  Triton gRPC: ModelInfer, ModelMetadata, …
                    ▼
     Serve gRPC proxy (Triton's servicer) on head and every worker
                    │  counted, balanced, autoscaled by Serve
                    ▼
   ┌─────────────────────────────────────────────────┐   × replicas.min…max
   │ worker pod                                      │
   │   ray-worker  raylet advertising triton: 1,     │
   │               proxy, TritonProxy replica ──┐    │
   │   triton      1 GPU, /cvmfs read-only    ◀─┘    │  localhost gRPC
   └─────────────────────────────────────────────────┘
                    ▲ replica demand
   ┌─────────────────────────────────────────────────┐
   │ head pod: Serve controller, Ray autoscaler      │  (0 CPUs for work, no GPU)
   └─────────────────────────────────────────────────┘
```

A **pod is one Triton on one GPU**, the unit a Triton deployment scales by. A
**replica is one pod**: every worker advertises one `triton` resource and
every replica claims one, so a replica lands next to its Triton and nowhere
else. Nothing else claims the resource, which is what leaves a pod without a
replica idle and therefore reclaimable, and a replica without a pod pending —
the request that grows the group.

Both containers share the pod's network namespace, so Triton's HTTP port is
moved off Ray Serve's; the port layout and the chart's render-time checks on
it are documented at `triton.httpPort` in the chart's
[`values.yaml`](../../helm/sonic-ray/values.yaml).

## What it serves and speaks

Whatever Triton is pointed at. On the AF that is the models CMSSW ships:
[`sonic-ray/values.yaml`](sonic-ray/values.yaml) mounts the cluster's CVMFS
claim read-only at `/cvmfs` and gives Triton `--model-repository` directories
inside a CMSSW release with an explicit load list. A new CMSSW release, or a
different model set, is a path change in those values. Every backend,
`config.pbtxt` semantics, dynamic batching and the repository index work as in
any Triton. Probe timings are chart defaults, overridden per release in the
values.

The wire protocol is Triton's gRPC. HTTP is **not** carried (Serve's HTTP
proxy answers only its own `/-/healthz` and `/-/routes`); Triton's HTTP port
stays inside the pod. CMSSW's `TritonClient` speaks gRPC, so `cmsRun` jobs
point at `sonic-ray-serve` as at any Triton endpoint; `tritonclient.grpc`
works the same way.

The one RPC not forwarded is `ModelStreamInfer`, Triton's bidirectional
stream: Serve's proxy carries unary and server-streaming calls only. CMSSW
uses the unary `ModelInfer`.

## Autoscaling

Two loops, both Ray's, nothing else in between:

1. **Ray Serve** sizes the deployment from the requests its gRPC proxy
   forwards. When the average number in flight per replica exceeds
   `serve.targetOngoingRequests` for `upscaleDelayS` it adds a replica; when
   it falls well below for `downscaleDelayS` it removes one, giving in-flight
   requests `gracefulShutdownTimeoutS`. Bounds are `replicas.min`/`max`.
2. **The Ray autoscaler** sizes the cluster. A new replica needs a `triton`
   resource; if no worker has one free, that is a pending request and the
   autoscaler adds a pod to `gpu-group` (ceiling: the same `replicas.max`; the
   group's own floor is 0, since Serve's minimum keeps pods alive). A worker
   whose replica is gone idles for `idleTimeoutSeconds` and is reclaimed; the
   pod then gets `terminationGracePeriodSeconds` against Triton's
   `--exit-timeout-secs` to drain (the chart refuses to render if the first is
   not larger).

One pair of numbers, `replicas` in
[`sonic-ray/values.yaml`](sonic-ray/values.yaml), sizes both, because a
replica _is_ a pod.

A replica only becomes ready once its Triton answers `ServerReady`, and it
polls `ServerLive` as its health check, so Serve never routes to a pod whose
Triton is still loading or has died — Serve restarts the replica, and Ray
reclaims a pod that stays broken.

## Compared with the Envoy + KEDA Triton deployments

The Triton deployments in [`apps/sonic`](../sonic) run the same server behind
Envoy with KEDA scaling on a Prometheus expression. Here Serve's gRPC proxy
takes Envoy's place behind a `LoadBalancer` on the same private pool, Ray
Serve's request-based autoscaling takes KEDA's, and Serve's
`maxOngoingRequests` back-pressure stands in for Envoy's rate limiter. Node
placement and the `scrape_metrics` label are the same;
`sonic-ray-triton-metrics` carries Triton's `nv_*` series and
`sonic-ray-metrics` Ray's, including `ray_serve_*`.

## Using it

```bash
kubectl -n cms get svc sonic-ray-serve            # MetalLB address on the private pool
```

CMSSW clients point at that address and the gRPC port, the way they point at
any Triton endpoint. From Python:

```python
import tritonclient.grpc as grpcclient

client = grpcclient.InferenceServerClient("<address>:<port>")
client.is_server_ready()
client.get_model_repository_index()
```

The Ray dashboard, for Serve and autoscaler state:

```bash
kubectl -n cms port-forward svc/sonic-ray-head-svc 8265:8265
```

## Deploy-time additions

The Ray containers run the image `ray.image` and `ray.version` name; the
Triton container runs `triton.image`. Two things are added at deploy time:

- **the forwarder** — `files/sonic_ray/*.py` become the `sonic-ray-code`
  ConfigMap, mounted at `/serve_app/sonic_ray` on head and workers. Its hash
  is annotated onto both pod templates, so a code change rolls the cluster.
- **Triton's Python stubs** — `python.pip` is pip-installed `--no-deps
--target` into an emptyDir by an init container on every pod, and that
  directory is on `PYTHONPATH`. Serve's proxies import the servicer from it at
  startup, on every node. Every pod start therefore needs PyPI reachable.

## Cost

`replicas.min` GPUs idle on the same `cms-af-prod` nodes the other Triton
deployments and the user sessions compete for. An upgrade costs a second set
for its duration: `upgradeStrategy: NewCluster` brings a second cluster up
before cutting over, and if no GPU is free it waits while the old one keeps
serving.
