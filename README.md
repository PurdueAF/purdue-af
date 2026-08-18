# Purdue Analysis Facility

[![CI](https://github.com/PurdueAF/purdue-af/actions/workflows/ci.yml/badge.svg)](https://github.com/PurdueAF/purdue-af/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/PurdueAF/purdue-af/graph/badge.svg)](https://codecov.io/gh/PurdueAF/purdue-af)
[![Docs deploy](https://github.com/PurdueAF/purdue-af/actions/workflows/docs-deploy.yml/badge.svg)](https://purdueaf.github.io/purdue-af/)
[![Registry GC](https://github.com/PurdueAF/purdue-af/actions/workflows/registry-gc.yml/badge.svg)](https://github.com/PurdueAF/purdue-af/actions/workflows/registry-gc.yml)
[![platform][platform-version]](https://github.com/PurdueAF/purdue-af/releases)
[![AF image][af-image-version]](RELEASING.md)

GitOps source of truth for the **Purdue Analysis Facility** — a Kubernetes-based interactive analysis platform for high energy physics research at CMS experiment.

Everything the cluster runs is declared here and reconciled by Flux; images and manifests are published only after the full CI/CD pipeline passes on the same commit.

User documentation:
[analysis-facility.physics.purdue.edu](https://analysis-facility.physics.purdue.edu)
Admin documentation: [https://purdue-cms-tier2.gitlab.io/documentation](https://purdue-cms-tier2.gitlab.io/documentation)

## Platform at a glance

|                  |                                                                                                                                                                                                                                                                                                                       |
| ---------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Orchestration    | [Kubernetes](https://github.com/kubernetes/kubernetes) on the [Geddes](https://www.rcac.purdue.edu/compute/geddes) cluster; [Flux](https://github.com/fluxcd/flux2) CD from two channels                                                                                                                              |
| Sessions         | [JupyterHub](https://github.com/jupyterhub/zero-to-jupyterhub-k8s) — [JupyterLab](https://github.com/jupyterlab/jupyterlab) and [code-server](https://github.com/coder/code-server) interfaces, [CILogon](https://www.cilogon.org) auth (Purdue / CERN / FNAL identities)                                             |
| Scale-out        | [Dask Gateway](https://github.com/dask/dask-gateway) — Kubernetes and Slurm backends                                                                                                                                                                                                                                  |
| User environment | [nvidia/cuda:12.4.1-devel-rockylinux8](https://hub.docker.com/r/nvidia/cuda) base image, [pixi](https://github.com/prefix-dev/pixi) environments                                                                                                                                                                      |
| Data             | [CVMFS](https://github.com/cvmfs/cvmfs), [XRootD](https://github.com/xrootd/xrootd), [XCache](https://github.com/opensciencegrid/xcache), [EOS](https://github.com/cern-eos/eos), [Depot](https://www.rcac.purdue.edu/storage/depot) NFS; [ServiceX](https://github.com/ssl-hep/ServiceX) for columnar delivery       |
| Inference        | [SuperSONIC](https://github.com/fastmachinelearning/SuperSONIC)                                                                                                                                                                                                                                                       |
| Observability    | [Prometheus](https://github.com/prometheus/prometheus), [Grafana](https://github.com/grafana/grafana), [Loki](https://github.com/grafana/loki), [Tempo](https://github.com/grafana/tempo), [Pyroscope](https://github.com/grafana/pyroscope), [Alloy](https://github.com/grafana/alloy) + two purpose-built exporters |
| Agents           | MCP server exposing AF-specific tools to any MCP client                                                                                                                                                                                                                                                               |

## Component status

Whether each component on the cluster is running what is on `main`
([![awaiting deployment][status-pending]](https://github.com/PurdueAF/purdue-af/releases)).

**Core** — the newest platform tag, currently ![platform][platform-version]

![af-users-graph][core-af-utils-af-users-graph]
![infrastructure][core-infrastructure]
![af-x509-secrets][core-jupyterhub-af-x509-secrets]
![database-backup][core-jupyterhub-database-backup]
![jupyterhub][core-jupyterhub-jupyterhub]
![jupyterhub-ssh][core-jupyterhub-jupyterhub-ssh]
![userlist-sync][core-jupyterhub-userlist-sync]
![af-monitoring][core-monitoring-af-monitoring]
![grafana][core-monitoring-grafana]
![prometheus][core-monitoring-prometheus]

**Experimental** — `main-validated`

![pixi-global-sync][experimental-af-utils-pixi-global-sync]
![slurm-probes][experimental-af-utils-slurm-probes]
![agentic-interface][experimental-agentic-interface]
![dask-gateway-k8s][experimental-dask-gateway-dask-gateway-k8s]
![dask-gateway-k8s-interlink][experimental-dask-gateway-dask-gateway-k8s-interlink]
![dask-gateway-k8s-slurm][experimental-dask-gateway-dask-gateway-k8s-slurm]
![infrastructure][experimental-infrastructure]
![interlink-gautschi][experimental-interlink-gautschi]
![interlink-hammer][experimental-interlink-hammer]
![interlink-negishi][experimental-interlink-negishi]
![af-monitoring][experimental-monitoring-af-monitoring]
![alloy][experimental-monitoring-alloy]
![loki][experimental-monitoring-loki]
![pyroscope][experimental-monitoring-pyroscope]
![tempo][experimental-monitoring-tempo]
![servicex][experimental-servicex]
![servicex-anvil][experimental-servicex-anvil]
![servicex-eos][experimental-servicex-eos]
![servicex-s3][experimental-servicex-s3]
![servicex-test][experimental-servicex-test]
![supersonic][experimental-sonic-supersonic]
![supersonic-dev][experimental-sonic-supersonic-dev]
![model-manager][experimental-sonic-model-manager]

**Images** — `purdue-af` is released on its own semver stream and pinned at
![AF image][af-image-version]; most aux images ride `:latest`.
`interlink-slurm-plugin` is pinned to its upstream plugin ref (`PLUGIN_REF`).

![purdue-af][image-purdue-af]
![agentic-interface][image-agentic-interface]
![af-pod-monitor][image-af-pod-monitor]
![af-node-monitor][image-af-node-monitor]
![supersonic-model-manager][image-supersonic-model-manager]
![interlink-slurm-plugin][image-interlink-slurm-plugin]

Reading the badges:

- `deployed` — no drift, the cluster has it
- `awaiting release` — validated, and only a release stands between it and
  the cluster
- `validating` — CI has not finished on those commits yet
- `failed CI` — do not release; the drift is broken
- the trailing number is how many commits it has moved since it was deployed

Recomputed hourly and after every CI run by
[`component-status.yml`](.github/workflows/component-status.yml); the badge
data lives on the [`status`](https://github.com/PurdueAF/purdue-af/tree/status)
branch, so keeping it current never touches `main`.

How a change reaches the cluster, version rules and rollback:
[RELEASING.md](RELEASING.md).

[core-af-utils-af-users-graph]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/core-af-utils-af-users-graph.json
[core-infrastructure]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/core-infrastructure.json
[core-jupyterhub-af-x509-secrets]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/core-jupyterhub-af-x509-secrets.json
[core-jupyterhub-database-backup]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/core-jupyterhub-database-backup.json
[core-jupyterhub-jupyterhub]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/core-jupyterhub-jupyterhub.json
[core-jupyterhub-jupyterhub-ssh]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/core-jupyterhub-jupyterhub-ssh.json
[core-jupyterhub-userlist-sync]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/core-jupyterhub-userlist-sync.json
[core-monitoring-af-monitoring]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/core-monitoring-af-monitoring.json
[core-monitoring-grafana]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/core-monitoring-grafana.json
[core-monitoring-prometheus]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/core-monitoring-prometheus.json
[experimental-af-utils-pixi-global-sync]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-af-utils-pixi-global-sync.json
[experimental-af-utils-slurm-probes]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-af-utils-slurm-probes.json
[experimental-agentic-interface]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-agentic-interface.json
[experimental-dask-gateway-dask-gateway-k8s]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-dask-gateway-dask-gateway-k8s.json
[experimental-dask-gateway-dask-gateway-k8s-interlink]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-dask-gateway-dask-gateway-k8s-interlink.json
[experimental-dask-gateway-dask-gateway-k8s-slurm]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-dask-gateway-dask-gateway-k8s-slurm.json
[experimental-infrastructure]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-infrastructure.json
[experimental-interlink-gautschi]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-interlink-gautschi.json
[experimental-interlink-hammer]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-interlink-hammer.json
[experimental-interlink-negishi]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-interlink-negishi.json
[experimental-monitoring-af-monitoring]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-monitoring-af-monitoring.json
[experimental-monitoring-alloy]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-monitoring-alloy.json
[experimental-monitoring-loki]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-monitoring-loki.json
[experimental-monitoring-pyroscope]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-monitoring-pyroscope.json
[experimental-monitoring-tempo]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-monitoring-tempo.json
[experimental-servicex]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-servicex.json
[experimental-servicex-anvil]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-servicex-anvil.json
[experimental-servicex-eos]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-servicex-eos.json
[experimental-servicex-s3]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-servicex-s3.json
[experimental-servicex-test]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-servicex-test.json
[experimental-sonic-model-manager]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-sonic-model-manager.json
[experimental-sonic-supersonic]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-sonic-supersonic.json
[experimental-sonic-supersonic-dev]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-sonic-supersonic-dev.json
[image-purdue-af]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/image-purdue-af.json
[image-agentic-interface]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/image-agentic-interface.json
[image-af-pod-monitor]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/image-af-pod-monitor.json
[image-af-node-monitor]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/image-af-node-monitor.json
[image-supersonic-model-manager]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/image-supersonic-model-manager.json
[image-interlink-slurm-plugin]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/image-interlink-slurm-plugin.json
[status-pending]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/_pending.json
[platform-version]: https://img.shields.io/github/v/tag/PurdueAF/purdue-af?filter=2*&sort=semver&label=platform&color=blue
[af-image-version]: https://img.shields.io/badge/dynamic/yaml?url=https%3A%2F%2Fraw.githubusercontent.com%2FPurdueAF%2Fpurdue-af%2Fmain%2Fapps%2Fjupyterhub%2Fjupyterhub%2Fvalues.yaml&query=%24.singleuser.image.tag&label=AF%20image&color=blue
