# Ray on the Analysis Facility

The SuperSONIC model repository, served by **Ray Serve** instead of Triton:
one Ray Serve deployment loads every ONNX model of the repository onto a GPU
and answers the KServe v2 HTTP API (the protocol Triton's HTTP clients speak);
Ray Serve runs as many replicas as the request load calls for, and the Ray
autoscaler adds a GPU pod for each replica that has nowhere to run.

This is the minimal shape: one chart, one deployment, **no custom image** —
the pods run the official Ray CUDA image, the server code reaches them as a
ConfigMap, and ONNX Runtime is installed by Ray Serve's `runtime_env` when a
replica starts. It scales from one GPU to as many as the worker group allows,
runs beside the `supersonic` release on the same model repository, and does
not replace it.

| path | what it is |
| --- | --- |
| `helmrepo.yaml` | `HelmRepository` for the KubeRay charts |
| `operator/` | `kuberay-operator` 1.7.0 — the `ray.io` CRDs and the controller. Namespaced (`singleNamespaceInstall: true`), so both the watch and the RBAC stay in `cms`. |
| `sonic-ray/chart/` | the `sonic-ray` chart: a `RayService` running the Serve application, the ConfigMap carrying the code, and a metrics Service |
| `sonic-ray/chart/files/sonic_ray/` | the server: `repository.py` (Triton-layout repository + ONNX Runtime), `protocol.py` (KServe v2 wire format), `serve_app.py` (the Ray Serve deployment) |
| `sonic-ray/helmrelease.yaml`, `sonic-ray/values.yaml` | the AF release: `dependsOn` the operator, supersonic's claim, placement and address pool |
| [`tests/sonic_ray/`](../../tests/sonic_ray), [`tests/manifests/test_ray.py`](../../tests/manifests/test_ray.py) | unit tests for the server (CPU onnxruntime, models built in the tests) and for the chart |

The chart lives here (like `apps/sonic/model-manager`) rather than being a raw
`RayService` because of ordering: until the operator's chart has installed the
`ray.io` CRDs that is an unknown kind, and kustomize-controller aborts an apply
on the first one it meets — on a fresh cluster, before the HelmRelease that
would install them. `dependsOn: kuberay-operator` is the fix, and a
`HelmRelease` is the only object that can carry it.

## Shape

```
     clients ──▶ sonic-ray-serve (LoadBalancer, private pool) :8000
                    │  KServe v2 HTTP: /v2/models/<name>/infer, …
                    ▼
   ┌─────────────────────────────────────────────┐   × 1…4 pods
   │ worker pod  (1 GPU, 8 CPU, 16Gi, /models ro) │
   │   Serve proxy → SonicServer replica          │
   │                 every ONNX model, ORT+CUDA   │
   └─────────────────────────────────────────────┘
          ▲ replica demand           │ ray_serve_* metrics :8080
          │                          ▼
   ┌─────────────────────────────────────────────┐
   │ head pod: Serve controller, Ray autoscaler  │  (0 CPUs for work, no GPU)
   └─────────────────────────────────────────────┘
```

A **replica is a server**: one GPU with every servable model loaded, the same
unit a Triton pod is. A **pod is a replica**: the worker group hands out
exactly one GPU per pod, and the chart refuses to render otherwise.

## What it serves

The repository is `supersonic-model-repository`, the claim the
[model manager](../sonic/model-manager) writes and the `supersonic` Triton
reads — mounted read-only at `/models` on the workers. Every directory with a
`config.pbtxt` is a model; the ones whose platform is `onnxruntime_onnx` are
loaded with ONNX Runtime (CUDA execution provider, CPU fallback), the rest are
**listed but not served**:

| model | platform | here |
| --- | --- | --- |
| `particleNetFromMiniAODAK4CHSCentral`, `…AK4CHSForward`, `…AK4PuppiCentral`, `…AK4PuppiForward`, `…AK8`, `particlenet` | `onnxruntime_onnx` | served |
| `higgsInteractionNet` | `onnxruntime_onnx` | served |
| `deepmet`, `deeptau_2017v2p1`, `deeptau_2018v2p5` | `tensorflow_graphdef` | listed `UNAVAILABLE` with the reason |

`POST /v2/repository/index` reports exactly that, per model, with the version
picked (highest numeric version directory, as Triton's default policy does).
Adding a TensorFlow runtime to the image is the obvious next step if those
three are wanted here.

Model metadata comes from `config.pbtxt` (batch dimension omitted when
`max_batch_size > 0`), because that is the contract the clients were written
against; inference validates input names, dtypes and the batch size the way
Triton does and answers 400 with the reason.

## Protocol

KServe v2 over HTTP, including the **binary tensor extension**
(`Inference-Header-Content-Length`, `binary_data_size`, `binary_data`) that
`tritonclient.http` uses by default — so a Python client written against the
supersonic release works with the endpoint swapped:

```python
import numpy as np, tritonclient.http as http

client = http.InferenceServerClient("<sonic-ray-serve address>:8000")
client.get_model_repository_index()
inputs = [http.InferInput("pf_features", [3, 32, 100], "FP32"), ...]
inputs[0].set_data_from_numpy(np.zeros((3, 32, 100), np.float32))
client.infer("particleNetFromMiniAODAK8", inputs).as_numpy("output")
```

What is **not** here, and why it matters: **gRPC**. CMSSW's SONIC client
(`TritonClient`) speaks gRPC only, so this endpoint serves Python, Coffea and
curl clients, not `cmsRun` jobs. Ray Serve has a gRPC proxy that takes a
user-defined servicer; implementing `GRPCInferenceService` on it is the piece
that would close that gap.

Also not here: cross-request dynamic batching (Triton's `dynamic_batching`).
Clients batch per request already (a `pf_features` tensor is a batch of jets);
`@serve.batch` per model is the follow-up if replicas turn out GPU-idle under
many small requests.

## Autoscaling

Two loops, both Ray's, nothing else in between:

1. **Ray Serve** sizes the deployment. Every replica reports its in-flight
   requests; when the average exceeds `serve.targetOngoingRequests` (8) for
   `upscaleDelayS` (10 s) Serve adds a replica, and when it falls well below
   for `downscaleDelayS` (300 s) it removes one — retired replicas get
   `gracefulShutdownTimeoutS` (60 s) to finish what they hold. Bounds are
   `serve.minReplicas`/`maxReplicas` (1/4 on the AF).
2. **The Ray autoscaler** sizes the cluster. A new replica needs one GPU; if
   no worker has a free one, that is a pending resource request and the
   autoscaler adds a pod to `gpu-group` (bounded by `ray.worker.minReplicas`/
   `maxReplicas`, 0/4). A worker whose replica is gone idles for
   `idleTimeoutSeconds` (60 s) and is reclaimed.

The chart ties the two together: `serve.maxReplicas` may not exceed
`ray.worker.maxReplicas`, or the extra replicas could never be placed. Raising
the GPU ceiling is one edit in `sonic-ray/values.yaml`:

```yaml
ray: { worker: { maxReplicas: 8 } }
serve: { maxReplicas: 8 }
```

Scale-to-zero (`serve.minReplicas: 0`) works, at the cost of the first
request waiting for a pod, an image pull and the model load.

## How it lines up with SuperSONIC

| SuperSONIC (`supersonic`) | Ray (`sonic-ray`) |
| --- | --- |
| Triton, every backend, gRPC + HTTP | Ray Serve + ONNX Runtime, HTTP (KServe v2 incl. binary tensors) |
| model repository PVC at `/models`, `--load-model=*` | same PVC, read-only, every `onnxruntime_onnx` model |
| Envoy behind a `LoadBalancer` on `geddes-private-pool` | KubeRay's serve Service, same pool |
| KEDA `ScaledObject` on a Prometheus expression, 1–10 Triton pods | Ray Serve request-based autoscaling, 1–4 replicas/GPUs |
| `nodeSelector: cms-af-prod=true` + the `hub.jupyter.org/dedicated` toleration | same, head and workers |
| Triton `nv_*` metrics scraped by label | Ray `ray_serve_*` metrics (request counts, latencies, queue depth per deployment) scraped by the same label, `release="sonic-ray"` |

`tests/manifests/test_ray.py` asserts the shared parts — claim, mount path,
placement, address pool — against `apps/sonic/supersonic/values.yaml` itself.

## Using it

```bash
kubectl -n cms get svc sonic-ray-serve            # MetalLB address on the private pool
SONIC=<address>:8000

curl -s "http://$SONIC/v2/health/ready" -o /dev/null -w '%{http_code}\n'
curl -s -X POST "http://$SONIC/v2/repository/index" | jq
curl -s "http://$SONIC/v2/models/particleNetFromMiniAODAK8" | jq
```

The Ray dashboard, for Serve and autoscaler state:

```bash
kubectl -n cms port-forward svc/sonic-ray-head-svc 8265:8265
```

Server-side configuration is by environment variable, set by the chart on
every container: `MODEL_REPOSITORY`, `ONNX_EXECUTION_PROVIDERS`, `MODELS`
(comma-separated allowlist of directories to load), `LOG_LEVEL`.

## Image, or rather none

The pods run `rayproject/ray:2.52.0-py312-cu128` (through the geddes Docker
Hub proxy cache) exactly as published: Ray with every extra, CUDA 12.8, cuDNN
9. Two things are added at deploy time instead of build time:

- **the code** — `files/sonic_ray/*.py` become the `sonic-ray-code` ConfigMap,
  mounted at `/serve_app/sonic_ray` on head and workers with
  `PYTHONPATH=/serve_app`. Their hash is annotated onto both pod templates, so
  a code change rolls the cluster.
- **ONNX Runtime** — `serve.pip` (`onnxruntime-gpu==1.26.0`, the last CUDA 12
  build; 1.27+ is CUDA 13) goes into the Serve application's `runtime_env`.
  Ray installs it into a virtualenv that inherits the image's packages the
  first time a replica starts on a pod, and reuses it for the pod's life. It
  dlopens the image's CUDA and cuDNN libraries.

The price is paid at pod start: after the image pull, a new pod downloads the
wheel (~300 MB) and installs it before its replica can load models — expect a
minute or two per scale-up, and a dependency on PyPI being reachable from the
GPU nodes. That trade was chosen deliberately over maintaining a custom Ray
image; if startup latency matters later, baking the wheel into an image is a
contained change (the code already imports onnxruntime lazily).

The chart's `ray.version` selects the image tag *and* pins the autoscaler
sidecar KubeRay adds, so the two cannot drift.

## Cost

One GPU idles (`serve.minReplicas: 1`) on the same `cms-af-prod` nodes
SuperSONIC and the user sessions compete for. An upgrade costs a second set
for its duration: `upgradeStrategy: NewCluster` brings a second cluster up
before cutting over, and if no GPU is free it waits while the old one keeps
serving.
