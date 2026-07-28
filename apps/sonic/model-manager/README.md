# SuperSONIC Model Manager

Web UI for the Triton model repository of a [SuperSONIC](https://fastmachinelearning.org/SuperSONIC/)
release: PVC usage, model upload, per-model metrics, and a switch to load/unload each model on
every Triton replica. Self-contained (one chart, one image, no database) so it can later be merged
into the SuperSONIC chart.

| Path | Contents |
| --- | --- |
| `apps/sonic/model-manager/` | Helm chart, HelmRelease, cluster values |
| `docker/supersonic-model-manager/` | application source and Dockerfile |
| `tests/supersonic_model_manager/` | unit tests |

Models that Triton loaded from elsewhere (CVMFS, another PVC) appear tagged **server only** — they
can be loaded and unloaded, but not deleted.

## Requirements

- Triton started with `--model-control-mode=explicit`, or it refuses runtime load/unload (the
  dashboard surfaces the refusal).
- The same claim mounted into the Triton pods as a model repository, otherwise Triton cannot see
  uploads. `ReadWriteMany` is required when both mount it.
- A Prometheus scraping Triton — only for the metric columns.

## Deployment

Flux deploys it from [`deploy/experimental/`](../../../deploy/experimental/kustomization.yaml) into
the `cms` namespace, alongside the [`supersonic`](../supersonic/) release it manages. Credentials
are not in git — create the Secret once, or the HelmRelease keeps retrying:

```bash
kubectl -n cms create secret generic supersonic-model-manager-auth \
  --from-literal=username=admin --from-literal=password='CHOOSE-A-PASSWORD'
```

There is no ingress — reach the dashboard with:

```bash
kubectl -n cms port-forward svc/supersonic-model-manager 8080:80
```

Standalone install:

```bash
helm install model-manager ./chart -n cms -f values-supersonic.yaml --set auth.password='...'
```

`supersonicRelease` is the value worth setting: it derives the Triton pod selector
(`app.kubernetes.io/component=triton,app.kubernetes.io/instance=<release>`) and the Prometheus
matcher (`release="<release>"`). The latter must be overridden when a ServiceMonitor-based
Prometheus scrapes Triton without adding a `release` label — as in `values-supersonic.yaml`.

To let Triton serve what you upload, point it at the same claim — this is how
[`apps/sonic/supersonic/values.yaml`](../supersonic/values.yaml) is wired:

```yaml
triton:
  modelRepository: { enabled: true, storageType: pvc, mountPath: /models, pvc: { claimName: supersonic-model-repository } }
  args:
    - |
      /opt/tritonserver/bin/tritonserver --model-repository=/models \
      --model-control-mode=explicit --load-model=*
```

## Configuration

Every chart value maps to an environment variable, so the image also runs standalone.

| Value | Env var | Default |
| --- | --- | --- |
| `modelRepository.claimName` | `PVC_NAME` | `supersonic-model-repository` |
| `modelRepository.mountPath` | `MODEL_REPOSITORY_PATH` | `/models` |
| `modelRepository.subPath` | *(volume mount)* | *(empty)* |
| `modelRepository.readOnly` | `READ_ONLY` | `false` |
| `modelRepository.maxUploadBytes` | `MAX_UPLOAD_BYTES` | 8 GiB |
| `triton.labelSelector` | `TRITON_LABEL_SELECTOR` | derived from `supersonicRelease` |
| `triton.endpoints` | `TRITON_ENDPOINTS` | *(empty; setting it forces static discovery)* |
| `triton.httpPort` | `TRITON_HTTP_PORT` | `8000` |
| `prometheus.url` / `.selector` / `.window` | `PROMETHEUS_URL` / `_SELECTOR` / `_WINDOW` | *(empty)* / derived / `5m` |
| `auth.enabled` / `.username` / `.password` | `AUTH_ENABLED` / `AUTH_USERNAME` / `AUTH_PASSWORD` | `true` / `admin` / *(required)* |
| `inferenceEndpoint` | `INFERENCE_ENDPOINT` | *(empty — discovered from the Envoy ingress/service)* |
| `refreshSeconds` | `REFRESH_SECONDS` | `15` |

`subPath` mounts a subdirectory of the claim as the repository root, for when models live inside a
larger shared claim. The gauge always shows how full the *claim* is, with the models' own
footprint next to the model count.

The PVC carries `helm.sh/resource-policy: keep` and is created only if absent, so uninstalling
never deletes models.

CephFS (and any CSI driver whose `fsGroupPolicy` is not `File`) ignores `podSecurityContext.fsGroup`
on ReadWriteMany volumes, so the claim mounts as `root:root 0755` and the unprivileged app cannot
write. `modelRepository.fixOwnership` (default true) runs a one-shot init container that chowns the
mount to the app's uid. Set it false when the volume is already writable.

## Uploads

Accepts a `.zip`/`.tar.*` archive or a directory (picker or drag-and-drop), landing as
`<model>/config.pbtxt` + `<model>/1/model.onnx`. Streamed straight to the PVC; extraction rejects
absolute paths, `..` traversal and symlinks, and drops `._*`/`__MACOSX`/`.DS_Store` noise.

Each upload is checked against Triton's layout rules before install, so malformed models are
rejected at the dashboard (HTTP 422, all problems at once) instead of silently failing to load:
missing/empty/zero version directories, files dropped outside a version directory, a
`config.pbtxt` `name:` that does not match the directory, and a version missing the artifact its
platform requires (`model.onnx`, `model.plan`, `model.savedmodel/`, … honouring
`default_model_filename`). Missing `config.pbtxt` is a warning for autocompletable backends and an
error otherwise. See `validation.py` for the full rule set.

## Security

Basic auth guards everything except `/healthz`. Credentials come from a Secret (`<release>-auth`,
or `auth.existingSecret`). `modelRepository.readOnly=true` gives a view-only instance. RBAC is one
namespaced Role: `get`/`list` on pods, PVCs, services and ingresses.

## Development

```bash
cd ../../../docker/supersonic-model-manager
pip install -r requirements.txt
MODEL_REPOSITORY_PATH=/tmp/models TRITON_ENDPOINTS=127.0.0.1:8000 AUTH_ENABLED=false \
  uvicorn model_manager.main:app --reload --port 8080
```

Outside a cluster there is no Kubernetes API access: use `TRITON_ENDPOINTS` for discovery, and PVC
capacity falls back to `statvfs`. The app calls the API server directly over `httpx` rather than
using the official client, which pulls in `cryptography` — whose Rust extension aborts the
interpreter on import in this image.

## CI

Built like the other aux images (see [`docker/REGISTRY.md`](../../../docker/REGISTRY.md)):
`image-inputs.sh` declares its input set, `ci-images.yml` builds from the repo root and smoke-tests
the image, `ci.yml` publishes `:sha-<commit>` and `:latest` (which the cluster pulls), and
`ci-checks.yml` runs the test suite.

```bash
docker build -f docker/supersonic-model-manager/Dockerfile -t supersonic-model-manager:dev .
uv run --project tests pytest -c tests/pyproject.toml tests/supersonic_model_manager
```

## Credits

The mark in `model_manager/static/img/` comes from
[fastmachinelearning/SuperSONIC](https://github.com/fastmachinelearning/SuperSONIC)
(`docs/img/SuperSONIC_small.svg`, `SuperSONIC_small_light.svg`). The dark variant is unmodified;
the light-theme one has its opaque white background rect removed so the mark is transparent.
