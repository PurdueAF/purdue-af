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
| `custom-spawner.py` auth gates            | singleuser image → upstream sample (tiny)  |
| OAuth code flow, auth_state, KubeSpawner  | storage/nodeSelectors/registry → nulled    |
| spawn → JupyterLab HTTP response          | `set-user-info.py` LDAP (phase 2, glauth)  |

## Covered behaviors

Login (allowed / denied / unknown idp / cern suffixing), spawn to a running
JupyterLab, ownership labels, multi-user isolation, explicit profile
selection landing in the pod, stop + cleanup, **userlist secret hot-reload**
(the userlist-sync pipeline's core assumption), admin_users wiring, forged
OAuth state rejection, logout.

## Run it

CI: the **Hub E2E** workflow — on changes to `apps/jupyterhub/**` + weekly.
To test a chart upgrade before unpinning Renovate:

    gh workflow run e2e-hub.yml -f chart_version=4.3.5

Locally (needs docker + kind + helm + kubectl + flux):

    tests/e2e_hub/setup-kind.sh          # ~3 min; CHART_VERSION=x.y.z to override
    E2E_HUB=1 uv run --project tests pytest tests/e2e_hub
    kind delete cluster --name af-e2e

## Phase 2 (not yet implemented)

- `set-user-info.py` in the loop: needs an LDAP mock (glauth) and an
  env-overridable LDAP host in the script (currently hardcoded).
- agentic-interface deployed as hub service: MCP login → session tools e2e.
- pixi env *build* validation stays in the separate `pixi-check` job
  (lock consistency only — the multi-GB image build remains kaniko's job).
