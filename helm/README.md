# Helm charts

Charts written here, versioned by Git rather than by `Chart.yaml`, and consumed
by a `HelmRelease` under [`apps/`](../apps) that carries the AF's values.

| chart                       | released by                                                                              |
| --------------------------- | ---------------------------------------------------------------------------------------- |
| [`sonic-ray/`](./sonic-ray) | [`apps/ray/sonic-ray/`](../apps/ray/sonic-ray) — see [its README](../apps/ray/README.md) |

`helm/<chart>/values.yaml` holds the chart's documented defaults; the AF's own
values live beside its HelmRelease in `apps/` and arrive through a
kustomize-generated ConfigMap. The HelmRelease sources the chart from the
experimental `GitRepository`; the settings every in-repo chart's HelmRelease
needs are in [`.claude/rules/manifests.md`](../.claude/rules/manifests.md).
`apps/sonic/model-manager/chart/` is an in-repo chart kept beside its release.

## Checks

`helm template` runs over every in-repo chart in
[`validate-manifests.sh`](../.github/workflows/validate-manifests.sh), against
the values the release actually deploys —
[`tests/manifests/`](../tests/manifests) then asserts on what came out.
