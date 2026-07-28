# SuperSONIC Model Manager

A small Kubernetes app that sits next to a [SuperSONIC](https://fastmachinelearning.org/SuperSONIC/)
release and gives it a web UI for managing the Triton model repository.

It is deliberately self-contained (one Helm chart, one image, no database) so it can later be
merged into the SuperSONIC chart as another component alongside `triton`, `envoy` and
`metrics-collector`.

Following this repository's layout, the component lives in two places:

| Path | Contents |
| --- | --- |
| `apps/sonic/model-manager/` | this README, the Helm chart, `helmrelease.yaml` and cluster values |
| `docker/supersonic-model-manager/` | application source and Dockerfile |
| `tests/supersonic_model_manager/` | unit tests (run by `ci-checks.yml` with every other suite) |

It is deployed as an **experimental** component alongside the `supersonic`
release it manages ([`apps/sonic/supersonic/`](../supersonic/)) — see
[Deployment](#deployment) below.

## What it does

- **Owns a model repository PVC** (`supersonic-model-repository`, 1 Gi by default). The PVC is
  created only if it does not already exist, and carries `helm.sh/resource-policy: keep`, so
  uninstalling the chart never deletes your models.
- **Serves a dashboard** through an Ingress, showing:
  - a gauge of how full the PVC is (used / total, %),
  - drag-and-drop upload of a model directory or `.zip`/`.tar.gz` archive,
  - every model known to the system — both the ones on this PVC *and* the ones the Triton
    servers loaded from somewhere else (CVMFS, another PVC, a baked-in repository),
  - per-model size, versions, backend, and Prometheus metrics (throughput, queue latency,
    compute latency, totals),
  - an iOS-style switch per model that loads/unloads it on every Triton replica.
- **Fans control calls out to all Triton replicas.** SuperSONIC's Triton Service is headless, so
  the app discovers pods by label and talks to each replica's HTTP port directly.
- **Shows the inference endpoint** clients should send requests to, discovered from the release's
  Envoy Ingress (falling back to the Envoy Service's in-cluster address). Click to copy.

## Requirements

| Requirement | Why |
| --- | --- |
| Triton started with `--model-control-mode=explicit` | Without it, Triton refuses runtime load/unload. The dashboard surfaces the refusal and disables the switch for that server. |
| The PVC mounted into the Triton pods as a model repository | Otherwise Triton cannot see anything you upload. |
| `ReadWriteMany` storage class (when Triton and the manager share the claim) | The manager writes while Triton reads. `ReadWriteOnce` only works if every pod lands on one node. |
| A Prometheus scraping Triton | Only needed for the metric columns; everything else works without it. |

## Deployment

Flux deploys it from [`deploy/experimental/`](../../../deploy/experimental/kustomization.yaml)
into the `cms` namespace: `helmrelease.yaml` sources the chart from the
`purdue-af-experimental` GitRepository, with `values-supersonic.yaml` supplied through the
`supersonic-model-manager-config` ConfigMap.

Credentials are **not** in git. Create the Secret once, or the HelmRelease keeps retrying:

```bash
kubectl -n cms create secret generic supersonic-model-manager-auth \
  --from-literal=username=admin --from-literal=password='CHOOSE-A-PASSWORD'
```

## Install manually

```bash
helm install model-manager ./chart \
  --namespace sonic \
  --set supersonicRelease=cms-run3-miniaod \
  --set auth.password='choose-a-password' \
  --set prometheus.url=http://cms-run3-miniaod-prometheus-server:9090 \
  --set ingress.enabled=true \
  --set ingress.hostName=sonic-models.geddes.rcac.purdue.edu \
  --set ingress.ingressClassName=public
```

`supersonicRelease` is the one value worth setting: it derives both the Triton pod selector
(`app.kubernetes.io/component=triton,app.kubernetes.io/instance=<release>`) and the Prometheus
label matcher (`release="<release>"`).

See [`values-geddes.yaml`](values-geddes.yaml) for a full example matching the
`cms-run3-miniaod` release in the `sonic` namespace on Geddes.

### Wiring it into SuperSONIC

Point the SuperSONIC release at the same claim and enable explicit model control:

```yaml
triton:
  modelRepository:
    enabled: true
    storageType: pvc
    mountPath: /models
    pvc:
      claimName: supersonic-model-repository
  args:
    - |
      /opt/tritonserver/bin/tritonserver \
      --model-repository=/models \
      --model-control-mode=explicit \
      --load-model=* \
      --exit-timeout-secs=60
```

A Triton server can hold several `--model-repository` flags at once, so this can be added
alongside an existing CVMFS repository. Models coming from those other repositories show up in
the table tagged **server only** — they can be loaded and unloaded, but not deleted, because
they do not live on the PVC.

## Uploading models

The dashboard accepts either shape, and both end up as a standard Triton layout on the PVC:

```
<model_name>/
  config.pbtxt
  1/
    model.onnx
```

- **An archive** (`.zip`, `.tar.gz`, `.tgz`, `.tar.bz2`, `.tar.xz`). The model directory may be
  at the archive root or wrapped one level deep; both are handled.
- **A directory**, via the folder picker or by dropping it onto the drop zone.

Uploads are streamed straight to the PVC rather than buffered in memory, extraction rejects
absolute paths, `..` traversal and symlinks, and macOS/Windows archive noise (`._*`, `__MACOSX`,
`.DS_Store`) is dropped. An upload that would replace an existing model is refused unless
*Replace an existing model* is checked.

### Validation

Every upload is checked against Triton's model repository rules *before* it is installed, so a
malformed model is rejected at the dashboard instead of silently failing to load later. Errors
block the upload (HTTP 422, all problems listed at once); warnings are shown but let it through,
because some rules depend on server flags the app cannot see.

**Rejected:**

- no numeric version directory — including the common case of model files dropped straight into
  the model directory (`model.onnx` instead of `1/model.onnx`)
- a version directory named `0` or a non-positive integer
- an empty version directory (except for `platform: "ensemble"`, where it is legal)
- `name:` in `config.pbtxt` not matching the directory the model is installed as
- the version directory missing the artifact the declared platform requires — `model.onnx` for
  `onnxruntime_onnx`, `model.plan` for `tensorrt_plan`, `model.savedmodel/` for
  `tensorflow_savedmodel`, `model.pt` for `pytorch_libtorch`, and so on, honouring
  `default_model_filename` when set
- `model.savedmodel` present as a file rather than a directory (and vice versa)
- no `config.pbtxt` for a backend Triton cannot autocomplete, or one whose backend cannot be
  inferred from the files at all

**Warned:**

- no `config.pbtxt` for an autocompletable backend (works only with `--strict-model-config=false`)
- non-numeric directories or stray files that Triton will ignore
- `model.xml` without a matching `model.bin` (OpenVINO)

Label files (`*.txt`) and `*.json` next to `config.pbtxt` are expected and pass without comment —
the CMS models on CVMFS ship `preprocess.json` and `*_labels.txt` this way.

## Configuration

Every chart value maps to an environment variable, so the image also runs standalone.

| Value | Env var | Default |
| --- | --- | --- |
| `modelRepository.claimName` | `PVC_NAME` | `supersonic-model-repository` |
| `modelRepository.mountPath` | `MODEL_REPOSITORY_PATH` | `/models` |
| `modelRepository.readOnly` | `READ_ONLY` | `false` |
| `modelRepository.maxUploadBytes` | `MAX_UPLOAD_BYTES` | `8589934592` (8 GiB) |
| `triton.discovery` | `TRITON_DISCOVERY` | `kubernetes` |
| `triton.labelSelector` | `TRITON_LABEL_SELECTOR` | derived from `supersonicRelease` |
| `triton.endpoints` | `TRITON_ENDPOINTS` | *(empty; setting it forces static discovery)* |
| `triton.httpPort` | `TRITON_HTTP_PORT` | `8000` |
| `prometheus.url` | `PROMETHEUS_URL` | *(empty — metrics disabled)* |
| `prometheus.selector` | `PROMETHEUS_SELECTOR` | derived from `supersonicRelease` |
| `prometheus.window` | `PROMETHEUS_WINDOW` | `5m` |
| `auth.enabled` / `auth.username` / `auth.password` | `AUTH_ENABLED` / `AUTH_USERNAME` / `AUTH_PASSWORD` | `true` / `admin` / *(required)* |
| `inferenceEndpoint` | `INFERENCE_ENDPOINT` | *(empty — discovered from the Envoy ingress/service)* |
| `refreshSeconds` | `REFRESH_SECONDS` | `15` |

`modelRepository.subPath` mounts a subdirectory of the claim as the repository root, which is how
the `supersonic` release is wired: its models live in `triton_models/` on the AF-wide
`af-shared-storage` claim. The gauge then shows how full the *claim* is, with the models' own
footprint listed next to the model count.

### Metrics

Per-model columns come from instant queries aggregated by Triton's `model` label:

| Column | Query |
| --- | --- |
| Throughput | `sum by (model) (rate(nv_inference_request_success[5m]))` |
| Queue | `sum by (model) (rate(nv_inference_queue_duration_us[5m])) / <throughput>` |
| Compute | `sum by (model) (rate(nv_inference_compute_infer_duration_us[5m])) / <throughput>` |

## Security

HTTP Basic auth guards the entire app — dashboard, API and uploads — with only `/healthz` left
open for kubelet probes. Credentials live in a Secret (`<release>-auth`, or bring your own with
`auth.existingSecret`). Set `modelRepository.readOnly=true` for a view-only instance.

RBAC is a namespaced Role granting `get`/`list` on pods (Triton discovery) and PVCs (capacity
reporting) — nothing else.

## Development

```bash
cd ../../../docker/supersonic-model-manager
pip install -r requirements.txt
MODEL_REPOSITORY_PATH=/tmp/models \
TRITON_ENDPOINTS=127.0.0.1:8000 \
PROMETHEUS_URL=http://127.0.0.1:9090 \
AUTH_ENABLED=false \
uvicorn model_manager.main:app --reload --port 8080
```

Outside a cluster there is no Kubernetes API access, so use `TRITON_ENDPOINTS` for discovery;
PVC capacity then comes from `statvfs` instead of the claim, and the inference endpoint from
`INFERENCE_ENDPOINT`.

The app talks to the Kubernetes API directly over `httpx` with the pod's service account rather
than using the official client library, which pulls in `google-auth` and `cryptography` — a
dependency tree whose Rust extension aborted the interpreter (SIGILL) on import in this image.
Only two calls are needed (list pods, read a PVC), plus two more for endpoint discovery.

### Layout

```
docker/supersonic-model-manager/model_manager/
  main.py          FastAPI routes: /api/state, /api/upload, load/unload, delete
  repository.py    PVC filesystem: scan, usage, safe extraction, install, delete
  triton.py        Pod discovery, /v2/repository/index, load/unload fan-out
  metrics.py       Prometheus instant queries per model
  validation.py    Triton model repository layout rules applied to uploads
  kube.py          Pod listing and PVC capacity (degrades to no-op off-cluster)
  auth.py          Basic auth middleware
  static/index.html  The dashboard (single file, no build step)
  static/img/      SuperSONIC mark, copied from the SuperSONIC repository

apps/sonic/model-manager/chart/   Helm chart
```

## CI

The image is built like the other aux images (see
[`docker/REGISTRY.md`](../../../docker/REGISTRY.md)):

- `image-inputs.sh` declares `docker/supersonic-model-manager` as its input set, so the
  content-addressed `in-<hash>` tag changes only when the app actually changes.
- `ci-images.yml` builds it from the repository root and smoke-tests the image by importing the
  app and asserting the dashboard, its logo assets and the ASGI routes all ship inside it.
- `ci.yml` publishes `:sha-<commit>` and `:latest` after the pipeline is green; the cluster pulls
  `:latest` through the `ghcr-proxy-cache`.
- `ci-checks.yml` runs `tests/supersonic_model_manager/` along with every other suite.

Build it locally the same way CI does:

```bash
docker build -f docker/supersonic-model-manager/Dockerfile -t supersonic-model-manager:dev .
```

### Tests

```bash
uv run --project tests pytest -c tests/pyproject.toml tests/supersonic_model_manager
```

They cover the Triton layout rules, safe archive extraction (traversal, symlinks, size limits,
macOS `._*` noise), usage accounting on shared claims, load/unload fan-out and its partial
failures, Prometheus `NaN` handling, endpoint discovery, and the auth gate.

## Credits

The SuperSONIC mark in `docker/supersonic-model-manager/model_manager/static/img/` is copied
unmodified from
[fastmachinelearning/SuperSONIC](https://github.com/fastmachinelearning/SuperSONIC)
(`docs/img/SuperSONIC_small.svg` and `SuperSONIC_small_light.svg`), which the dashboard swaps
between light and dark themes.
