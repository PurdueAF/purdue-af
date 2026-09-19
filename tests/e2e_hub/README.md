# JupyterHub end-to-end tests (mock environment)

Spins up the **real** hub — exact chart version from `helmrelease.yaml`, real
`values.yaml`, byte-identical `extraFiles` auth scripts — inside a throwaway
[kind](https://kind.sigs.k8s.io/) cluster, with every external dependency
mocked. Never touches the production cluster.

## What is real vs mocked

| Real (identical to production)             | Mocked / replaced                             |
| ------------------------------------------ | --------------------------------------------- |
| z2jh chart version (from helmrelease.yaml) | CILogon → `mock-cilogon.py` (OAuth code flow) |
| `values.yaml` (flux-envsubst'd, like Flux) | userlist secrets → test users                 |
| every `jupyterhub_config.d` snippet        | LDAP → openldap seeded like geddes-auth       |
| OAuth code flow, auth_state, KubeSpawner   | storage/nodeSelectors/registry → nulled       |
| `ldap_lookup()` query/parse path           | Prometheus → absent (gpu script fails open)   |
| singleuser image (the real AF image)       |                                               |
| spawn → JupyterLab HTTP response           |                                               |

## Covered behaviors

Login (allowed / denied / unknown idp / cern suffixing), spawn to a running
JupyterLab, **LDAP uid/gid mapping landing in the pod env**, ownership
labels, multi-user isolation, explicit profile selection landing in the pod,
stop + cleanup, **userlist secret hot-reload** (the userlist-sync pipeline's
core assumption), admin_users wiring, forged OAuth state rejection, logout.

## Run it

CI runs it in [`ci-e2e.yml`](../../.github/workflows/ci-e2e.yml), against
the chart version in `helmrelease.yaml` and the hub configmaps of
`deploy/core-production/kustomization.yaml`. To validate a chart upgrade, bump
`helmrelease.yaml` in a PR.

Locally (needs docker + kind + helm + kubectl + flux):

> Apple Silicon: hub images of chart >= 4.3.5 ship a `cryptography` wheel
> (>= 47) whose arm64 build SIGILLs under Docker Desktop's VM — the hub
> crash-loops with exit 132 and empty logs. Test those versions in CI
> (amd64); locally, chart <= 4.3.0 works.

    tests/e2e_hub/setup-kind.sh          # ~3 min; CHART_VERSION=x.y.z to override
    E2E_HUB=1 uv run --project tests pytest tests/e2e_hub
    kind delete cluster --name af-e2e

## Pre-release image

The test that spawns the real purdue-af image through the hub's `pre-release`
profile is gated by `E2E_PRERELEASE=1`; `setup-kind.sh` pre-pulls whatever
`PRERELEASE_IMAGE` names onto the kind node. Which image CI passes is
[`ci-e2e.yml`](../../.github/workflows/ci-e2e.yml)'s. Locally:

    PRERELEASE_IMAGE=ghcr.io/purdueaf/purdue-af:pre-release tests/e2e_hub/setup-kind.sh
    E2E_HUB=1 E2E_PRERELEASE=1 PRERELEASE_IMAGE=ghcr.io/purdueaf/purdue-af:pre-release \
        uv run --project tests pytest tests/e2e_hub -k prerelease

## Not covered by this harness

- The agentic-interface is not deployed as a hub service here, so the MCP
  login → session-tool path is untested end to end.
- NetworkPolicies are rendered but not enforced by kind's default CNI, so
  policy regressions (e.g. hub egress to LDAP/CILogon) do not show up.
- The geddes pull-through cache is not exercised: images come from ghcr and
  upstream quay directly.
