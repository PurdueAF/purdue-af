# Purdue Analysis Facility

[![CI](https://github.com/PurdueAF/purdue-af/actions/workflows/ci.yml/badge.svg)](https://github.com/PurdueAF/purdue-af/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/PurdueAF/purdue-af/graph/badge.svg)](https://codecov.io/gh/PurdueAF/purdue-af)
[![Docs deploy](https://github.com/PurdueAF/purdue-af/actions/workflows/docs-deploy.yml/badge.svg)](https://purdueaf.github.io/purdue-af/)
[![Registry GC](https://github.com/PurdueAF/purdue-af/actions/workflows/registry-gc.yml/badge.svg)](https://github.com/PurdueAF/purdue-af/actions/workflows/registry-gc.yml)
[![Platform](https://img.shields.io/github/v/tag/PurdueAF/purdue-af?filter=2*&sort=semver&label=platform&color=B1810B)](https://github.com/PurdueAF/purdue-af/releases)
[![AF image](https://img.shields.io/badge/dynamic/yaml?url=https%3A%2F%2Fraw.githubusercontent.com%2FPurdueAF%2Fpurdue-af%2Fmain%2Fapps%2Fjupyterhub%2Fjupyterhub%2Fvalues.yaml&query=%24.singleuser.image.tag&label=AF%20image&color=B1810B)](RELEASING.md)

GitOps source of truth for the **Purdue Analysis Facility** — an interactive
CMS analysis platform on the Purdue Tier-2 (Geddes) Kubernetes cluster:
JupyterHub sessions on demand, Dask clusters that burst onto Kubernetes or
Slurm, ServiceX delivery, GPU inference, and the monitoring around it.

Everything the cluster runs is declared here and reconciled by Flux; images
and manifests are published only after the full pipeline passes on the same
commit. End-user documentation lives at
[analysis-facility.physics.purdue.edu](https://analysis-facility.physics.purdue.edu).

## Platform at a glance

|                  |                                                                                            |
| ---------------- | ------------------------------------------------------------------------------------------ |
| Orchestration    | Kubernetes (Geddes, RCAC), namespace `cms`; [Flux](https://fluxcd.io) CD from two channels |
| Sessions         | JupyterHub (z2jh chart 4.4.0), CILogon auth — Purdue / CERN / FNAL identities              |
| Scale-out        | Dask Gateway 2024.1.0 — Kubernetes backend + Slurm backends (Hammer, Gautschi)             |
| User environment | One ~5 GB CUDA image + [pixi](https://pixi.sh) environments, Python 3.12 throughout        |
| Data             | CVMFS, XRootD, EOS, `/depot` NFS; ServiceX for columnar delivery                           |
| Inference        | SuperSONIC (Triton-backed GPU inference for CMS workflows)                                 |
| Observability    | Prometheus, Grafana, Loki, Tempo, Pyroscope, Alloy + two purpose-built exporters           |
| Agents           | MCP server exposing session/Dask/storage/log tools to any MCP client                       |

## Repository map

```
apps/          what runs in the cluster — Helm releases + manifests, one dir per component
  jupyterhub/    hub values, auth gate, spawner hooks, GPU admission, userlist sync
  dask-gateway/  k8s + Slurm gateway releases (per-cluster values)
  monitoring/    prometheus · grafana · loki · tempo · pyroscope · alloy · exporters
  agentic-interface/  MCP server deployment
  af-utils/      shared-storage tooling, incl. the global pixi env reconciler
deploy/        Flux entry points; one kustomization per environment (see deploy/README.md)
docker/        first-party image sources (purdue-af, agentic-interface, monitors, …)
pixi/          environment definitions: base/ (baked into the image), global/ (on /work)
tests/         411 tests — unit, ASGI, hub-config, exporters, and hub-in-kind e2e
docs/          user documentation (Zensical → GitHub Pages)
```

## How changes reach the cluster

| Change                                            | Path to production                                                                               |
| ------------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| Core component (hub config, monitoring, cronjobs) | push to `main` → CI green → mint a platform tag → production Flux reconciles (~1 min)            |
| Experimental component                            | push to `main` → CI green → publish advances `main-validated` → Flux reconciles (~1 min)         |
| AF image content (Dockerfile, `pixi/base`)        | push to `main` → CI builds + e2e → `:pre-release` moves → release the image, then a platform tag |
| Global env (`pixi/global`)                        | push to `main` → CI validates the lock → `pixi-global-sync` applies it to `/work/pixi/global`    |
| Aux images (agentic-interface, monitors)          | push to `main` → CI green → `:latest` moves → pod restart picks it up                            |

Bump rules, rollback and the pipeline diagram: [RELEASING.md](RELEASING.md).

## Operations

```bash
# what production runs right now
yq '.singleuser.image.tag' apps/jupyterhub/jupyterhub/values.yaml
git tag -l '2*' | sort -V | tail -1

# cluster state (namespace cms)
kubectl -n cms get pods -l component=singleuser-server        # user sessions
kubectl -n cms get cronjobs                                   # userlist sync, x509, backups
flux -n cms get kustomizations                                # purdue-af-flux: applied revision
kubectl -n cms logs deploy/pixi-global-sync -f                # global env sync

# releases (Actions tab, or:)
gh workflow run release-image.yml -f bump=patch
gh workflow run release-platform.yml
```

## Working on it

```bash
uvx pre-commit run --all-files          # ruff · prettier · shfmt · shellcheck · hadolint · mypy
uv run --project tests pytest tests     # everything except the kind e2e
tests/e2e_hub/setup-kind.sh             # real hub in kind (~3 min), then:
E2E_HUB=1 uv run --project tests pytest tests/e2e_hub
.github/workflows/validate-manifests.sh # kustomize build + kubeconform + helm template + promtool
```

CI runs these same commands, driven by the same configs
(`.pre-commit-config.yaml`, `ruff.toml`, `mypy.ini`).

## Pointers

|                                                         |                                                                                      |
| ------------------------------------------------------- | ------------------------------------------------------------------------------------ |
| Release + versioning rules, pipeline diagram            | [RELEASING.md](RELEASING.md)                                                         |
| Deployment channels, how a change reaches the cluster   | [deploy/README.md](deploy/README.md)                                                 |
| Registry layout, tag taxonomy, ghcr ↔ geddes proxy      | [docker/REGISTRY.md](docker/REGISTRY.md)                                             |
| AF image internals, build analysis, promotion checklist | [docker/purdue-af/README.md](docker/purdue-af/README.md)                             |
| e2e harness — what is real vs mocked                    | [tests/e2e_hub/README.md](tests/e2e_hub/README.md)                                   |
| MCP agentic interface                                   | [apps/agentic-interface/README.md](apps/agentic-interface/README.md)                 |
| User documentation                                      | [analysis-facility.physics.purdue.edu](https://analysis-facility.physics.purdue.edu) |
