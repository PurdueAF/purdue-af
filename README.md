# Purdue Analysis Facility

[![CI](https://github.com/PurdueAF/purdue-af/actions/workflows/ci.yml/badge.svg)](https://github.com/PurdueAF/purdue-af/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/PurdueAF/purdue-af/graph/badge.svg)](https://codecov.io/gh/PurdueAF/purdue-af)
[![Docs deploy](https://github.com/PurdueAF/purdue-af/actions/workflows/docs-deploy.yml/badge.svg)](https://purdueaf.github.io/purdue-af/)
[![Registry GC](https://github.com/PurdueAF/purdue-af/actions/workflows/registry-gc.yml/badge.svg)](https://github.com/PurdueAF/purdue-af/actions/workflows/registry-gc.yml)
[![Platform](https://img.shields.io/github/v/tag/PurdueAF/purdue-af?filter=2*&sort=semver&label=platform&color=B1810B)](https://github.com/PurdueAF/purdue-af/releases)
[![AF image](https://img.shields.io/badge/dynamic/yaml?url=https%3A%2F%2Fraw.githubusercontent.com%2FPurdueAF%2Fpurdue-af%2Fmain%2Fapps%2Fjupyterhub%2Fjupyterhub%2Fvalues.yaml&query=%24.singleuser.image.tag&label=AF%20image&color=B1810B)](RELEASING.md)

GitOps source of truth for the **Purdue Analysis Facility** — a Kubernetes-based interactive analysis platform for high energy physics research at CMS experiment.

Everything the cluster runs is declared here and reconciled by Flux; images and manifests are published only after the full CI/CD pipeline passes on the same commit.

User documentation:
[analysis-facility.physics.purdue.edu](https://analysis-facility.physics.purdue.edu)
Admin documentation: [https://purdue-cms-tier2.gitlab.io/documentation](https://purdue-cms-tier2.gitlab.io/documentation)

## Platform at a glance

[Kubernetes](https://github.com/kubernetes/kubernetes) on the
[Geddes](https://www.rcac.purdue.edu/compute/geddes) cluster, reconciled by
[Flux](https://github.com/fluxcd/flux2) from two channels: **core** is
pinned to the newest platform tag, **experimental** follows `main-validated`
and ships as soon as CI is green.

The status column says whether the cluster is running what is on `main`
([![awaiting deployment][status-pending]](https://github.com/PurdueAF/purdue-af/releases)):

| Badge | Meaning |
| --- | --- |
| `deployed` | on the cluster, no drift |
| `awaiting release` | fully validated — needs a platform tag (or an image release) to ship |
| `validating` | CI has not finished on those commits yet |
| `failed CI` | do not release; the drift is broken |

The number after a status is how many commits that component has moved since
it was deployed.

| Area | Component | What it is | Status |
| --- | --- | --- | --- |
| Sessions | [`jupyterhub`](apps/jupyterhub/jupyterhub) | [JupyterHub](https://github.com/jupyterhub/zero-to-jupyterhub-k8s) — [JupyterLab](https://github.com/jupyterlab/jupyterlab) and [code-server](https://github.com/coder/code-server) profiles, [CILogon](https://www.cilogon.org) auth (Purdue / CERN / FNAL identities) | ![core][core-jupyterhub-jupyterhub] |
|  | [`purdue-af`](docker/purdue-af) | the session image: [nvidia/cuda:12.4.1-devel-rockylinux8](https://hub.docker.com/r/nvidia/cuda) base, [pixi](https://github.com/prefix-dev/pixi) `base` environment, OSG grid clients ([XRootD](https://github.com/xrootd/xrootd), gfal2) | ![core][core-docker-purdue-af] |
|  | [`jupyterhub-ssh`](apps/jupyterhub/jupyterhub-ssh) | SSH and SFTP access into a running session | ![core][core-jupyterhub-jupyterhub-ssh] |
|  | [`userlist-sync`](apps/jupyterhub/userlist-sync) | syncs the authorized Purdue and CERN user lists into the hub's secrets | ![core][core-jupyterhub-userlist-sync] |
|  | [`af-x509-secrets`](apps/jupyterhub/af-x509-secrets) | keeps the grid X.509 proxy secret current for ServiceX | ![core][core-jupyterhub-af-x509-secrets] |
|  | [`database-backup`](apps/jupyterhub/database-backup) | twice-daily backup of the hub database, 5 kept | ![core][core-jupyterhub-database-backup] |
| Environment | [`pixi-global-sync`](apps/af-utils/pixi-global-sync) | keeps the shared [pixi](https://github.com/prefix-dev/pixi) `global` environment on `/work/pixi/global` in step with `pixi/global/` | ![experimental][experimental-af-utils-pixi-global-sync] |
| Scale-out | [`dask-gateway-k8s`](apps/dask-gateway/dask-gateway-k8s) | [Dask Gateway](https://github.com/dask/dask-gateway), Kubernetes backend | ![experimental][experimental-dask-gateway-dask-gateway-k8s] |
|  | [`dask-gateway-k8s-slurm`](apps/dask-gateway/dask-gateway-k8s-slurm) | Dask Gateway, Slurm backend | ![experimental][experimental-dask-gateway-dask-gateway-k8s-slurm] |
|  | [`dask-gateway-k8s-slurm-hammer`](apps/dask-gateway/dask-gateway-k8s-slurm-hammer) | Dask Gateway, Slurm backend on Hammer | ![experimental][experimental-dask-gateway-dask-gateway-k8s-slurm-hammer] |
|  | [`dask-gateway-k8s-slurm-gautschi`](apps/dask-gateway/dask-gateway-k8s-slurm-gautschi) | Dask Gateway, Slurm backend on Gautschi | ![experimental][experimental-dask-gateway-dask-gateway-k8s-slurm-gautschi] |
|  | [`dask-gateway-k8s-interlink`](apps/dask-gateway/dask-gateway-k8s-interlink) | Dask Gateway, workers offloaded through interLink | ![experimental][experimental-dask-gateway-dask-gateway-k8s-interlink] |
| Storage | [`infrastructure`](apps/infrastructure) | the PVCs and pull secrets every session depends on: the [CVMFS](https://github.com/cvmfs/cvmfs) mount, the shared `/work` volume, registry credentials | ![core][core-infrastructure] ![experimental][experimental-infrastructure] |
| Data delivery | [`servicex`](apps/servicex) | [ServiceX](https://github.com/ssl-hep/ServiceX) — columnar delivery, in-cluster MinIO | ![experimental][experimental-servicex] |
|  | [`servicex-s3`](apps/servicex-s3) | ServiceX, Purdue S3 output | ![experimental][experimental-servicex-s3] |
|  | [`servicex-anvil`](apps/servicex-anvil) | ServiceX, Anvil S3 output | ![experimental][experimental-servicex-anvil] |
|  | [`servicex-eos`](apps/servicex-eos) | ServiceX, CMS Rucio input | ![experimental][experimental-servicex-eos] |
|  | [`servicex-test`](apps/servicex-test) | ServiceX, test instance | ![experimental][experimental-servicex-test] |
| Inference | [`supersonic`](apps/sonic/supersonic) | [SuperSONIC](https://github.com/fastmachinelearning/SuperSONIC) inference servers | ![experimental][experimental-sonic-supersonic] |
| Observability | [`prometheus`](apps/monitoring/prometheus) | [Prometheus](https://github.com/prometheus/prometheus) — metrics and alerting rules | ![core][core-monitoring-prometheus] |
|  | [`grafana`](apps/monitoring/grafana) | [Grafana](https://github.com/grafana/grafana) — dashboards, provisioned from this repo | ![core][core-monitoring-grafana] |
|  | [`af-monitoring`](apps/monitoring/af-monitoring) | two purpose-built exporters: per-session metrics and per-node health | ![core][core-monitoring-af-monitoring] ![experimental][experimental-monitoring-af-monitoring] |
|  | [`loki`](apps/monitoring/loki) | [Loki](https://github.com/grafana/loki) — logs | ![experimental][experimental-monitoring-loki] |
|  | [`tempo`](apps/monitoring/tempo) | [Tempo](https://github.com/grafana/tempo) — traces | ![experimental][experimental-monitoring-tempo] |
|  | [`pyroscope`](apps/monitoring/pyroscope) | [Pyroscope](https://github.com/grafana/pyroscope) — continuous profiling | ![experimental][experimental-monitoring-pyroscope] |
|  | [`alloy`](apps/monitoring/alloy) | [Alloy](https://github.com/grafana/alloy) — the collector feeding all of the above | ![experimental][experimental-monitoring-alloy] |
|  | [`af-users-graph`](apps/af-utils/af-users-graph) | cumulative registered-user plot, from the hub database backup | ![core][core-af-utils-af-users-graph] |
| Agents | [`agentic-interface`](apps/agentic-interface) | MCP server exposing AF-specific tools to any MCP client | ![experimental][experimental-agentic-interface] |
|  | [`kagent`](apps/kagent) | in-cluster agents with access to the AF's own Kubernetes API | ![experimental][experimental-kagent] |

Sessions also mount [EOS](https://github.com/cern-eos/eos) and
[Depot](https://www.rcac.purdue.edu/storage/depot) NFS from the hosts, and read
remote data through [XCache](https://github.com/opensciencegrid/xcache).

Status is recomputed hourly and after every CI run by
[`component-status.yml`](.github/workflows/component-status.yml); the badge
data lives on the [`status`](https://github.com/PurdueAF/purdue-af/tree/status)
branch, so keeping it current never touches `main`.

## How changes reach the cluster

| Change                                            | Path to production                                                                                                          |
| ------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| Core component (hub config, monitoring, cronjobs) | push to `main` → CI green → mint new platform tag (manual) → core-production Flux reconciles (~1 min)                            |
| AF image content (Dockerfile, `pixi/base`)        | push to `main` → CI builds + e2e → `:pre-release` moves → mint new image version (manual), then a new platform tag (manual) |
| Experimental component                            | push to `main` → CI green → publish advances `main-validated` → Flux reconciles (~1 min)                                    |
| Global env (`pixi/global`)                        | push to `main` → CI validates the lock → `pixi-global-sync` applies it to `/work/pixi/global`                               |
| Aux images (agentic-interface, monitors)          | push to `main` → CI green → `:latest` moves → pod restart picks it up                                                       |

Manual steps are `workflow_dispatch` runs from the Actions tab (`Release
platform`, `Release image`); everything else happens on its own once CI is
green. Version rules and rollback: [RELEASING.md](RELEASING.md).

[core-jupyterhub-jupyterhub]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/core-jupyterhub-jupyterhub.json
[core-docker-purdue-af]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/core-docker-purdue-af.json
[core-jupyterhub-jupyterhub-ssh]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/core-jupyterhub-jupyterhub-ssh.json
[core-jupyterhub-userlist-sync]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/core-jupyterhub-userlist-sync.json
[core-jupyterhub-af-x509-secrets]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/core-jupyterhub-af-x509-secrets.json
[core-jupyterhub-database-backup]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/core-jupyterhub-database-backup.json
[experimental-af-utils-pixi-global-sync]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-af-utils-pixi-global-sync.json
[experimental-dask-gateway-dask-gateway-k8s]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-dask-gateway-dask-gateway-k8s.json
[experimental-dask-gateway-dask-gateway-k8s-slurm]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-dask-gateway-dask-gateway-k8s-slurm.json
[experimental-dask-gateway-dask-gateway-k8s-slurm-hammer]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-dask-gateway-dask-gateway-k8s-slurm-hammer.json
[experimental-dask-gateway-dask-gateway-k8s-slurm-gautschi]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-dask-gateway-dask-gateway-k8s-slurm-gautschi.json
[experimental-dask-gateway-dask-gateway-k8s-interlink]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-dask-gateway-dask-gateway-k8s-interlink.json
[core-infrastructure]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/core-infrastructure.json
[experimental-infrastructure]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-infrastructure.json
[experimental-servicex]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-servicex.json
[experimental-servicex-s3]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-servicex-s3.json
[experimental-servicex-anvil]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-servicex-anvil.json
[experimental-servicex-eos]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-servicex-eos.json
[experimental-servicex-test]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-servicex-test.json
[experimental-sonic-supersonic]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-sonic-supersonic.json
[core-monitoring-prometheus]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/core-monitoring-prometheus.json
[core-monitoring-grafana]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/core-monitoring-grafana.json
[core-monitoring-af-monitoring]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/core-monitoring-af-monitoring.json
[experimental-monitoring-af-monitoring]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-monitoring-af-monitoring.json
[experimental-monitoring-loki]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-monitoring-loki.json
[experimental-monitoring-tempo]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-monitoring-tempo.json
[experimental-monitoring-pyroscope]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-monitoring-pyroscope.json
[experimental-monitoring-alloy]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-monitoring-alloy.json
[core-af-utils-af-users-graph]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/core-af-utils-af-users-graph.json
[experimental-agentic-interface]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-agentic-interface.json
[experimental-kagent]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-kagent.json
[status-pending]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/_pending.json
