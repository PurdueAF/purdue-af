# Purdue Analysis Facility

[![CI](https://github.com/PurdueAF/purdue-af/actions/workflows/ci.yml/badge.svg)](https://github.com/PurdueAF/purdue-af/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/PurdueAF/purdue-af/graph/badge.svg)](https://codecov.io/gh/PurdueAF/purdue-af)
[![Docs deploy](https://github.com/PurdueAF/purdue-af/actions/workflows/docs-deploy.yml/badge.svg)](https://purdueaf.github.io/purdue-af/)
[![Registry GC](https://github.com/PurdueAF/purdue-af/actions/workflows/registry-gc.yml/badge.svg)](https://github.com/PurdueAF/purdue-af/actions/workflows/registry-gc.yml)
[![Platform](https://img.shields.io/github/v/tag/PurdueAF/purdue-af?filter=2*&sort=semver&label=platform&color=B1810B)](https://github.com/PurdueAF/purdue-af/releases)
[![AF image](https://img.shields.io/badge/dynamic/yaml?url=https%3A%2F%2Fraw.githubusercontent.com%2FPurdueAF%2Fpurdue-af%2Fmain%2Fapps%2Fjupyterhub%2Fjupyterhub%2Fvalues.yaml&query=%24.singleuser.image.tag&label=AF%20image&color=B1810B)](RELEASING.md)

GitOps source of truth for the **Purdue Analysis Facility** — a Kubernetes-based interactive analysis platform for high energy physics research at CMS experiment.
Functionality includes JupyterHub sessions on demand, Dask clusters that burst onto Kubernetes or Slurm, data delivery via ServiceX, GPU inference-as-a-service, agentic AI interface, and the monitoring around it.

Everything the cluster runs is declared here and reconciled by Flux; images and manifests are published only after the full CI/CD pipeline passes on the same commit. End-user documentation lives at
[analysis-facility.physics.purdue.edu](https://analysis-facility.physics.purdue.edu).

## Platform at a glance

|                  |                                                                                                                                                                                                                                                                                                                       |
| ---------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Orchestration    | [Kubernetes](https://github.com/kubernetes/kubernetes) (Geddes cluster), namespace `cms`; [Flux](https://github.com/fluxcd/flux2) CD from two channels                                                                                                                                                                |
| Sessions         | [JupyterHub](https://github.com/jupyterhub/zero-to-jupyterhub-k8s) (z2jh chart), [CILogon](https://cilogon.org) auth — Purdue / CERN / FNAL identities                                                                                                                                                                |
| Scale-out        | [Dask Gateway](https://github.com/dask/dask-gateway) — Kubernetes + [Slurm](https://github.com/SchedMD/slurm) backends                                                                                                                                                                                                |
| User environment | [Rocky Linux 8](https://rockylinux.org) + CUDA image, [pixi](https://github.com/prefix-dev/pixi) environments                                                                                                                                                                                                         |
| Data             | [CVMFS](https://github.com/cvmfs/cvmfs), [XRootD](https://github.com/xrootd/xrootd), [EOS](https://github.com/cern-eos/eos), `/depot` NFS; [ServiceX](https://github.com/ssl-hep/ServiceX) for columnar delivery                                                                                                      |
| Inference        | [SuperSONIC](https://github.com/fastmachinelearning/SuperSONIC) — shared [Triton](https://github.com/triton-inference-server/server) servers, scaled per workload                                                                                                                                                     |
| Observability    | [Prometheus](https://github.com/prometheus/prometheus), [Grafana](https://github.com/grafana/grafana), [Loki](https://github.com/grafana/loki), [Tempo](https://github.com/grafana/tempo), [Pyroscope](https://github.com/grafana/pyroscope), [Alloy](https://github.com/grafana/alloy) + two purpose-built exporters |
| Agents           | [MCP](https://github.com/modelcontextprotocol/modelcontextprotocol) server exposing AF-specific tools to any MCP client                                                                                                                                                                                               |

## How changes reach the cluster

| Change                                            | Path to production                                                                                             |
| ------------------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| Core component (hub config, monitoring, cronjobs) | push to `main` → CI green → **run `Release platform`** → production Flux reconciles (~1 min)                   |
| AF image content (Dockerfile, `pixi/base`)        | push to `main` → CI builds + e2e → `:pre-release` moves → **run `Release image`**, then **`Release platform`** |
| Experimental component                            | push to `main` → CI green → publish advances `main-validated` → Flux reconciles (~1 min)                       |
| Global env (`pixi/global`)                        | push to `main` → CI validates the lock → `pixi-global-sync` applies it to `/work/pixi/global`                  |
| Aux images (agentic-interface, monitors)          | push to `main` → CI green → `:latest` moves → pod restart picks it up                                          |

**Bold** steps are the only manual ones: a `workflow_dispatch` run from the
Actions tab. Everything else happens on its own once CI is green — nothing
reaches production on a schedule or a timer.

## Pointers

|                                                         |                                                                      |
| ------------------------------------------------------- | -------------------------------------------------------------------- |
| Release + versioning rules, pipeline diagram            | [RELEASING.md](RELEASING.md)                                         |
| Deployment channels: core vs experimental               | [deploy/README.md](deploy/README.md)                                 |
| Registry layout, tag taxonomy, ghcr ↔ geddes proxy      | [docker/REGISTRY.md](docker/REGISTRY.md)                             |
| AF image internals, build analysis, promotion checklist | [docker/purdue-af/README.md](docker/purdue-af/README.md)             |
| e2e harness — what is real vs mocked                    | [tests/e2e_hub/README.md](tests/e2e_hub/README.md)                   |
| MCP agentic interface                                   | [apps/agentic-interface/README.md](apps/agentic-interface/README.md) |
