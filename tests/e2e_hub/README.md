# JupyterHub end-to-end tests (mock environment)

Spins up the **real** hub — exact chart version from `helmrelease.yaml`, real
`values.yaml`, byte-identical `extraFiles` auth scripts — inside a throwaway
[kind](https://kind.sigs.k8s.io/) cluster, with every external dependency
mocked. Never touches the production cluster.

## What is real vs mocked

| Real (identical to production)            | Mocked / replaced                          |
| ----------------------------------------- | ------------------------------------------ |
| z2jh chart version (from helmrelease.yaml) | CILogon → `mock-cilogon.py` (OAuth code flow) |
| `values.yaml` (flux-envsubst'd, like Flux) | userlist secrets → test users              |
| all 3 `jupyterhub_config.d` snippets      | LDAP → openldap seeded like geddes-aux     |
| OAuth code flow, auth_state, KubeSpawner  | singleuser image → upstream sample (tiny)  |
| `ldap_lookup()` query/parse path          | storage/nodeSelectors/registry → nulled    |
| spawn → JupyterLab HTTP response          | Prometheus → absent (gpu script fails open) |

## Covered behaviors

Login (allowed / denied / unknown idp / cern suffixing), spawn to a running
JupyterLab, **LDAP uid/gid mapping landing in the pod env**, ownership
labels, multi-user isolation, explicit profile selection landing in the pod,
stop + cleanup, **userlist secret hot-reload** (the userlist-sync pipeline's
core assumption), admin_users wiring, forged OAuth state rejection, logout.

## Run it

CI: the **Hub E2E** workflow — on changes to `apps/jupyterhub/jupyterhub/**`,
the production kustomization, or the harness itself, plus weekly. It always
tests **what the repo deploys**: the chart version comes from
`helmrelease.yaml` and the hub configmaps are derived from
`deploy/core-production/kustomization.yaml` — there is no version knob. To
validate a chart upgrade, bump `helmrelease.yaml` in a PR; the workflow
exercises that exact version and fails the PR if values or scripts break
the deployment.

Locally (needs docker + kind + helm + kubectl + flux):

> Apple Silicon: hub images of chart >= 4.3.5 ship a `cryptography` wheel
> (>= 47) whose arm64 build SIGILLs under Docker Desktop's VM — the hub
> crash-loops with exit 132 and empty logs. Test those versions in CI
> (amd64); locally, chart <= 4.3.0 works.

    tests/e2e_hub/setup-kind.sh          # ~3 min; CHART_VERSION=x.y.z to override
    E2E_HUB=1 uv run --project tests pytest tests/e2e_hub
    kind delete cluster --name af-e2e

## Remaining gaps (phase 3)

- agentic-interface deployed as hub service: MCP login → session tools e2e.
- pixi env *build* validation stays in the separate `pixi-check` job
  (lock consistency only — the multi-GB image build remains kaniko's job).
- NetworkPolicies are rendered but NOT enforced by kind's default CNI —
  policy regressions (e.g. hub egress to LDAP/CILogon) need the rendered
  manifest diff (see JUPYTERHUB_UPGRADE_PLAN.md phase 2.3) or staging.
- the geddes-registry pull-through cache is not exercised (upstream quay
  images are used); check mirror tags with `docker manifest inspect` before
  a version bump.
