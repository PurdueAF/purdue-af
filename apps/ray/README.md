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
| `sonic-ray/chart/` | the `sonic-ray` chart: a `RayService` with a Triton in every worker pod, the autoscaling controller, and the Services |
| `sonic-ray/chart/files/sonic_serve.py` | the controller — turns Triton queue depth into Ray resource demand |
| `sonic-ray/helmrelease.yaml`, `sonic-ray/values.yaml` | the AF release: `dependsOn` the operator, values with supersonic's `triton:` block |

The chart lives here (like `apps/sonic/model-manager`) rather than being a raw
`RayService` because of ordering: until the operator's chart has installed the
`ray.io` CRDs that is an unknown kind, and kustomize-controller aborts an apply
on the first one it meets — on a fresh cluster, before the HelmRelease that
would install them. `dependsOn: kuberay-operator` is the fix, and a
`HelmRelease` is the only object that can carry it.

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

## Autoscaling

Inference never touches a Ray task or actor, so Ray sees no demand and would
leave the group at its floor forever. `TritonAutoscaler` — one replica, pinned
to the head, holding no GPU — closes that loop every `control_interval_s`:

```
pending  = Σ nv_inference_pending_request_count over every live Triton,
           averaged over look_back_s
desired  = clamp(ceil(pending / target_pending_per_server), min, max)
request_resources(bundles=[{"triton": 1}] * desired)
```

`request_resources` is the Ray autoscaler's public "keep room for this" API.
Each worker advertises `triton: 1`, so a bundle *is* a GPU pod with a Triton in
it, and the autoscaler adds and removes pods to match. **Nothing else in the
cluster asks for that resource** — no Serve replicas on workers, no proxies
(`proxy_location: HeadOnly`), a bare raylet beside Triton — which is what
leaves an unwanted worker idle and therefore reclaimable.

Up is immediate. Down releases `downscale_step` pods per `downscale_delay_s`
of sustained low demand, since each one kills a Triton; the pod then gets
`terminationGracePeriodSeconds` against Triton's `--exit-timeout-secs` to
drain (the chart refuses to render if the first is not larger). The request is
re-asserted every tick, so a restarted controller starts from the cluster it
finds rather than from a stale floor left in GCS.

The policy is the chart's `autoscaling:` block, delivered as Serve
`user_config` — retuning it reconfigures the running replica without a
restart:

| key | default | meaning |
| --- | --- | --- |
| `min_servers` / `max_servers` | 1 / 10 | bounds; the worker group's `minReplicas`/`maxReplicas` are derived from them |
| `target_pending_per_server` | 16 | requests awaiting execution per Triton before another is asked for |
| `look_back_s` | 30 | averaging window for the queue gauge |
| `control_interval_s` | 10 | how often the decision is made |
| `downscale_delay_s` | 120 | sustained low demand required before giving a pod back |
| `downscale_step` | 1 | pods released per delay window |

`GET /` on the head's Serve port returns the last decision and what it was
based on:

```bash
kubectl -n cms port-forward svc/sonic-ray-head-svc 8000:8000
curl -s localhost:8000 | jq
```

Scale-to-zero is not supported: with no Triton there is no queue to read, so
`min_servers: 0` parks the release rather than sleeping it.

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

`tests/manifests/test_ray.py` asserts the parity against
`apps/sonic/supersonic/values.yaml` itself — including a token-by-token
comparison of Triton's arguments — so retuning supersonic and forgetting Ray
turns the build red. Against the rendered chart it also asserts the properties
the scaling loop depends on: the controller stays on the head and off the GPUs,
the workers stay free of Ray work, the Services select on labels KubeRay does
not overwrite, and the policy handed to the controller is one it reads.
`validate-manifests.sh` renders the chart with the AF values on every CI run.

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

The worker group idles at one GPU (`min_servers: 1`) on the same `cms-af-prod`
nodes SuperSONIC and the user sessions compete for. An upgrade costs one more
for its duration: `upgradeStrategy: NewCluster` brings a second cluster up
before cutting over, and if no GPU is free it waits while the old one keeps
serving. A GPU node here has 128 cores, so the extra CPU the Ray container
adds to each pod changes nothing about what fits.
