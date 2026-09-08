# Helm charts

Charts written here, versioned by Git rather than by `Chart.yaml`, and consumed
by a `HelmRelease` under [`apps/`](../apps) that carries the AF's values.

| chart                       | released by                                                                              | what it is                                                                                                    |
| --------------------------- | ---------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| [`sonic-ray/`](./sonic-ray) | [`apps/ray/sonic-ray/`](../apps/ray/sonic-ray) — see [its README](../apps/ray/README.md) | Triton Inference Server on Ray: Ray Serve's gRPC proxy carries Triton's protocol to a Triton in every GPU pod |

## Why a directory of its own

A chart here is a deliverable, not a manifest: nothing in it names the facility,
and its defaults have to stand on their own. Keeping it out of `apps/` — where
everything is specific to this cluster — is what makes that boundary visible,
and what makes `git filter-repo`-ing a chart into a repository of its own a
move rather than an untangling. `sonic-ray` is here on exactly that bet: if Ray
proves useful, the chart leaves; if it does not, it goes with the release.

Two rules follow from being sourced out of Git:

- **`reconcileStrategy: Revision`** in the HelmRelease. `Chart.yaml` keeps a
  static `0.1.0`, and Flux's default `ChartVersion` re-packages only when that
  string changes — so template edits would never reach the cluster while values
  edits still triggered an upgrade, running new values against the old chart.
- **Values belong to the release, not the chart.** `helm/<chart>/values.yaml`
  is the chart's documented defaults; the AF's own values live beside its
  HelmRelease in `apps/` and arrive through a kustomize-generated ConfigMap.

`apps/sonic/model-manager/chart/` predates this directory and still lives with
its release.

## Checks

`helm template` runs over every in-repo chart in
[`validate-manifests.sh`](../.github/workflows/validate-manifests.sh), against
the values the release actually deploys —
[`tests/manifests/`](../tests/manifests) then asserts on what came out.
