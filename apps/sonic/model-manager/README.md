# SuperSONIC Model Manager

Web UI for the Triton model repository of a [SuperSONIC](https://fastmachinelearning.org/SuperSONIC/)
release: PVC usage, model upload, per-model metrics, and a switch to load/unload each model on
every Triton replica. Self-contained: one chart, one image, no database.

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

Flux deploys it from [`flux/experimental/`](../../../flux/experimental/kustomization.yaml) into
the `cms` namespace, alongside the [`supersonic-af`](../supersonic-af/) release it manages, the
one SuperSONIC release with a PVC model repository. Credentials are not in git — create the
Secret once, or the HelmRelease keeps retrying:

```bash
kubectl -n cms create secret generic supersonic-model-manager-auth \
  --from-literal=username=admin --from-literal=password='CHOOSE-A-PASSWORD'
```

The dashboard is served at `supersonic-models.geddes.rcac.purdue.edu` (`ingress` in
`values-supersonic-af.yaml`). Without an ingress, port-forward:

```bash
kubectl -n cms port-forward svc/supersonic-model-manager 8080:80
```

`supersonicRelease` is the value worth setting: it derives the Triton pod selector
(`app.kubernetes.io/component=inference-server,app.kubernetes.io/instance=<release>`) and the Prometheus
matcher (`release="<release>"`). The latter must be overridden when a ServiceMonitor-based
Prometheus scrapes Triton without adding a `release` label.

To let Triton serve what you upload, point it at the same claim, as
[`apps/sonic/supersonic-af/values.yaml`](../supersonic-af/values.yaml) does.

## Configuration

Every chart value maps to an environment variable, so the image also runs standalone:
[`chart/values.yaml`](chart/values.yaml) lists the values and their defaults, and
[`config.py`](../../../docker/supersonic-model-manager/model_manager/config.py) the variables.

`subPath` mounts a subdirectory of the claim as the repository root, for when models live inside a
larger shared claim. The gauge always shows how full the *claim* is, with the models' own
footprint next to the model count.

## Uploads

Accepts a `.zip`/`.tar.*` archive or a directory (picker or drag-and-drop), landing as
`<model>/config.pbtxt` + `<model>/1/model.onnx`. Streamed straight to the PVC; extraction rejects
absolute paths, `..` traversal and symlinks, and drops `._*`/`__MACOSX`/`.DS_Store` noise.

Each upload is checked against Triton's layout rules before install, so a malformed model is
rejected at the dashboard with all its problems at once, not left failing to load. The rules are in
[`validation.py`](../../../docker/supersonic-model-manager/model_manager/validation.py).

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
capacity falls back to `statvfs`.

## Credits

The mark in `model_manager/static/img/` comes from
[fastmachinelearning/SuperSONIC](https://github.com/fastmachinelearning/SuperSONIC)
(`docs/img/SuperSONIC_small.svg`, `SuperSONIC_small_light.svg`). The dark variant is unmodified;
the light-theme one has its opaque white background rect removed so the mark is transparent.
