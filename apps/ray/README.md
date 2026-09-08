# Ray on the Analysis Facility

**Triton on Ray**: every worker pod carries NVIDIA's Triton Inference Server,
and **Ray Serve's gRPC proxy carries Triton's protocol** to it.
Serve speaks that protocol because it is handed Triton's own generated
servicer; the only code of ours is a forwarder that passes each RPC from the
proxy to the Triton in its pod. Serve counts every request on the way through,
sizes the deployment from that, and the Ray autoscaler adds a GPU pod for each
replica with nowhere to go.

No custom image, no protocol code, no model code: official Ray, a stock
Triton image, ~100 lines of glue shipped as a ConfigMap, Triton's Python stubs
pip-installed by an init container.

| path                                                                                                            | what it is                                                                                                                                                                     |
| --------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `helmrepo.yaml`                                                                                                 | `HelmRepository` for the KubeRay charts                                                                                                                                        |
| `operator/`                                                                                                     | `kuberay-operator` 1.7.0 — the `ray.io` CRDs and the controller. Namespaced (`singleNamespaceInstall: true`), so both the watch and the RBAC stay in `cms`.                    |
| [`helm/sonic-ray/`](../../helm/sonic-ray)                                                                       | the `sonic-ray` chart: a `RayService` with a Triton in every worker pod and the forwarder as its Serve application, the ConfigMap carrying the forwarder, two metrics Services |
| [`helm/sonic-ray/files/sonic_ray/serve_app.py`](../../helm/sonic-ray/files/sonic_ray/serve_app.py)              | the forwarder — one replica per pod, every unary RPC of `GRPCInferenceService` handed to the pod's Triton unchanged                                                            |
| `sonic-ray/helmrelease.yaml`, `sonic-ray/values.yaml`                                                           | the AF release: `dependsOn` the operator, the Triton image, arguments, resources and probes, and the CVMFS model repositories                                                  |
| [`tests/sonic_ray/`](../../tests/sonic_ray), [`tests/manifests/test_ray.py`](../../tests/manifests/test_ray.py) | source-level checks of the forwarder; rendered-chart checks of the pod, its ports, its probes and the scaling bounds                                                           |

This is a chart and not a raw `RayService` because of ordering: until the
operator's chart has installed the `ray.io` CRDs that is an unknown kind, and
kustomize-controller aborts an apply on the first one it meets — on a fresh
cluster, before the HelmRelease that would install them. `dependsOn:
kuberay-operator` is the fix, and a `HelmRelease` is the only object that can
carry it.

The chart itself sits in [`helm/`](../../helm) rather than beside its release
here, so that it can be lifted into a repository of its own if Ray earns a
place — see [`helm/README.md`](../../helm/README.md). What stays in this
directory is the AF deployment: the operator, and the release's own values.

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
   │   triton      1 GPU, 4 CPU, 16G, /cvmfs ro ◀┘   │  localhost:8001
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

### Ports

Both containers share the pod's network namespace, so they cannot both use
Triton's defaults:

| port | who                  | why it is where it is                                                                  |
| ---- | -------------------- | -------------------------------------------------------------------------------------- |
| 8000 | Ray Serve HTTP proxy | KubeRay probes the proxy here; it does not move                                        |
| 9000 | Ray Serve gRPC proxy | the inference entry point, behind the release's Service on 8001                        |
| 8080 | Ray metrics          | scraped by `sonic-ray-metrics`                                                         |
| 8100 | Triton HTTP          | **moved** off Triton's default 8000, which Ray holds; serves only the kubelet's probes |
| 8001 | Triton gRPC          | Triton's default; what the forwarder dials on localhost                                |
| 8002 | Triton metrics       | scraped by `sonic-ray-triton-metrics`                                                  |

Leaving Triton on 8000 is what makes it exit with `failed to start HTTP
service: Unavailable - Socket '0.0.0.0:8000' already in use`. The ports are
chart values (`triton.httpPort`, `triton.grpcPort`), and the chart refuses to
render if they collide with Ray's, if they disagree with the `--http-port` /
`--grpc-port` in `triton.args`, or if the args leave Triton on 8000.

## What it serves and speaks

Whatever Triton is pointed at. On the AF that is the models CMSSW ships:
`sonic-ray/values.yaml` mounts the cluster's CVMFS claim read-only at `/cvmfs`
and gives Triton four `--model-repository` directories inside a CMSSW release
(`CMSSW_17_0_0_pre2` today — RecoBTag, RecoEgamma, RecoTauTag, RecoMET) with
an explicit load list:

`deepmet`, `deeptau_2018v2p5`, `particleNetFromMiniAODAK4CHSCentral`,
`particleNetFromMiniAODAK4PuppiCentral`, `particleNetFromMiniAODAK4PuppiForward`,
`particleNetFromMiniAODAK8`, `particlenet_AK8_MD-2prong_PT`,
`particlenet_AK8_MassRegression_PT`, `particlenet_PT`,
`unifiedparticletransformer_AK4_V01`.

Nothing is uploaded anywhere and there is no model manager: a new CMSSW
release, or a different model set, is a path change in the values. Every
backend, `config.pbtxt` semantics, dynamic batching and the repository index
work as in any Triton, because it is Triton. The first load from CVMFS pulls
the files over the network into the node's cache, so the startup probe allows
four minutes; readiness then requires three consecutive successes, because
Triton answers ready while it is still loading the rest of the repository and
can flap back. The probe timings otherwise match the facility's other Triton
deployments, so a server judged healthy there is judged healthy here.

The wire protocol is Triton's gRPC. HTTP is **not** carried (Serve's HTTP
proxy on 8000 answers only its own `/-/healthz` and `/-/routes`); Triton's
HTTP port stays inside the pod. CMSSW's `TritonClient` speaks gRPC, so
`cmsRun` jobs point at `sonic-ray-serve:8001` as at any Triton endpoint — the
port is Triton's conventional one on purpose. `tritonclient.grpc` works the
same way.

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
   `replicas.min`/`max` (1/4 on the AF).
2. **The Ray autoscaler** sizes the cluster. A new replica needs a `triton`
   resource; if no worker has one free, that is a pending request and the
   autoscaler adds a pod to `gpu-group` (ceiling: the same `replicas.max`; the
   group's own floor is 0, since a pod with a replica on it is never idle and
   Serve's minimum therefore keeps pods alive). A worker whose replica is gone idles for
   `idleTimeoutSeconds` (60 s) and is reclaimed; the pod then gets
   `terminationGracePeriodSeconds` against Triton's `--exit-timeout-secs` to
   drain (the chart refuses to render if the first is not larger).

One pair of numbers sizes both, because a replica _is_ a pod. Raising the GPU
ceiling is one edit in `sonic-ray/values.yaml`:

```yaml
replicas: { min: 1, max: 8 }
```

A replica only becomes ready once its Triton answers `ServerReady`, and it
polls `ServerLive` as its health check, so Serve never routes to a pod whose
Triton is still loading or has died — Serve restarts the replica, and Ray
reclaims a pod that stays broken.

## How it lines up with Triton behind Envoy and KEDA

The facility's other Triton deployments put Envoy in front and KEDA on the
side. This is the same server, reached and scaled differently:

| Triton behind Envoy + KEDA                                                              | Ray (`sonic-ray`)                                                                                                         |
| --------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| Triton on a per-site PVC or CVMFS, explicit load list                                   | Triton on CVMFS, explicit load list — a plain `--model-repository` path                                                   |
| Envoy: gRPC entry point behind a `LoadBalancer` on `geddes-private-pool`, `ROUND_ROBIN` | Serve's gRPC proxy behind KubeRay's serve Service, same pool, port 8001                                                   |
| `ingress.enabled: false` — private pool only                                            | no ingress; the head is `ClusterIP`, dashboard by port-forward only                                                       |
| KEDA `ScaledObject` on a Prometheus expression, 1–10 pods                               | Ray Serve request-based autoscaling, 1–4 pods — see above                                                                 |
| `nodeSelector: cms-af-prod=true` + the `hub.jupyter.org/dedicated` toleration           | same, head and workers                                                                                                    |
| model repository from a PVC or CVMFS                                                    | the cluster's `cvmfs` claim, mounted **read-only**                                                                        |
| Triton Service labelled `scrape_metrics: "true"`                                        | `sonic-ray-triton-metrics` (`nv_*`) and `sonic-ray-metrics` (Ray, incl. `ray_serve_*`), same label, `release="sonic-ray"` |
| Envoy's Lua rate limiter on `RepositoryIndex`                                           | none; Serve's `maxOngoingRequests` back-pressure instead                                                                  |

## Using it

```bash
kubectl -n cms get svc sonic-ray-serve            # MetalLB address on the private pool
SONIC=<address>:8001
```

CMSSW clients point at `$SONIC`, the way they point at any Triton endpoint.
From Python:

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
Docker Hub proxy cache) exactly as published; the Triton container runs the
image the values name (the chart default is `nvcr.io/nvidia/tritonserver`;
the AF values use `fastml/triton-torchgeo:26.04-py3-geometric`, which
carries the PyTorch, TensorFlow, ONNX and torch-geometric backends every
model in the load list needs). Two things are added at deploy time instead of
build time:

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

One GPU idles (`replicas.min: 1`) on the same `cms-af-prod` nodes the other
Triton deployments and the user sessions compete for. That floor also hides the worst
of the scale-up latency: a pod on a node that has never pulled the Triton
image waits on 7.8 GB before the model load even begins. An upgrade costs a second set
for its duration: `upgradeStrategy: NewCluster` brings a second cluster up
before cutting over, and if no GPU is free it waits while the old one keeps
serving. A GPU node here has 128 cores, so the two extra CPUs the Ray
container adds to each pod change nothing about what fits.
