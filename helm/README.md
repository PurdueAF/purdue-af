# Helm charts

Charts written here, versioned by Git rather than by `Chart.yaml`, and consumed
by a `HelmRelease` under [`apps/`](../apps) that carries the AF's values.

| chart                       | released by                                                                              | what it is                                                                                                    |
| --------------------------- | ---------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| [`sonic-ray/`](./sonic-ray) | [`apps/ray/sonic-ray/`](../apps/ray/sonic-ray) — see [its README](../apps/ray/README.md) | Triton Inference Server on Ray: Ray Serve's gRPC proxy carries Triton's protocol to a Triton in every GPU pod |

## What belongs here

A chart here names no facility: its templates and its defaults stand on their
own, so the directory can be lifted into a repository of its own unchanged.
Anything specific to this cluster — the release, its values, its `dependsOn` —
belongs beside the `HelmRelease` in `apps/`.

Two conventions come with being sourced out of Git:

- **`reconcileStrategy: Revision`** in the HelmRelease. `Chart.yaml` keeps a
  static `0.1.0`, and Flux's default `ChartVersion` re-packages only when that
  string changes: template edits never reach the cluster, while values edits
  still trigger an upgrade, running new values against the old chart.
- **Values belong to the release, not the chart.** `helm/<chart>/values.yaml`
  holds the chart's documented defaults; the AF's own values live beside its
  HelmRelease in `apps/` and arrive through a kustomize-generated ConfigMap.

`apps/sonic/model-manager/chart/` predates this directory and still lives with
its release.

## Checks

`helm template` runs over every in-repo chart in
[`validate-manifests.sh`](../.github/workflows/validate-manifests.sh), against
the values the release actually deploys —
[`tests/manifests/`](../tests/manifests) then asserts on what came out.
