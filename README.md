# Purdue Analysis Facility

[![CI](https://github.com/PurdueAF/purdue-af/actions/workflows/ci.yml/badge.svg)](https://github.com/PurdueAF/purdue-af/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/PurdueAF/purdue-af/graph/badge.svg)](https://codecov.io/gh/PurdueAF/purdue-af)
[![Docs deploy](https://github.com/PurdueAF/purdue-af/actions/workflows/docs-deploy.yml/badge.svg)](https://analysis-facility.physics.purdue.edu)
[![Registry GC](https://github.com/PurdueAF/purdue-af/actions/workflows/registry-gc.yml/badge.svg)](https://github.com/PurdueAF/purdue-af/actions/workflows/registry-gc.yml)
[![platform][platform-version]](https://github.com/PurdueAF/purdue-af/releases)
[![AF image][af-image-version]](RELEASING.md)

GitOps source of truth for the **Purdue Analysis Facility** — a Kubernetes-based interactive analysis platform for high energy physics research at CMS experiment.

Everything the cluster runs is declared here and reconciled by Flux.

User documentation:
[analysis-facility.physics.purdue.edu](https://analysis-facility.physics.purdue.edu)
Admin documentation: [https://purdue-cms-tier2.gitlab.io/documentation](https://purdue-cms-tier2.gitlab.io/documentation)

## Platform at a glance

|                  |                                                                                                                                                                                                                                                                                                                       |
| ---------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Orchestration    | [Kubernetes](https://github.com/kubernetes/kubernetes) on the [Geddes](https://www.rcac.purdue.edu/compute/geddes) cluster; [Flux](https://github.com/fluxcd/flux2) CD ([roots](deploy/README.md))                                                                                                                    |
| Sessions         | [JupyterHub](https://github.com/jupyterhub/zero-to-jupyterhub-k8s) — [JupyterLab](https://github.com/jupyterlab/jupyterlab) and [code-server](https://github.com/coder/code-server) interfaces, [CILogon](https://www.cilogon.org) auth                                                                               |
| Scale-out        | [Dask Gateway](https://github.com/dask/dask-gateway) — Kubernetes and Slurm backends                                                                                                                                                                                                                                  |
| User environment | the [`purdue-af` image](docker/purdue-af/README.md), [pixi](https://github.com/prefix-dev/pixi) environments                                                                                                                                                                                                          |
| Data             | [CVMFS](https://github.com/cvmfs/cvmfs), [XRootD](https://github.com/xrootd/xrootd), [XCache](https://github.com/opensciencegrid/xcache), [EOS](https://github.com/cern-eos/eos), [Depot](https://www.rcac.purdue.edu/storage/depot) NFS; [ServiceX](https://github.com/ssl-hep/ServiceX) for columnar delivery       |
| Inference        | [SuperSONIC](https://github.com/fastmachinelearning/SuperSONIC)                                                                                                                                                                                                                                                       |
| Observability    | [Prometheus](https://github.com/prometheus/prometheus), [Grafana](https://github.com/grafana/grafana), [Loki](https://github.com/grafana/loki), [Tempo](https://github.com/grafana/tempo), [Pyroscope](https://github.com/grafana/pyroscope), [Alloy](https://github.com/grafana/alloy) + purpose-built exporters     |
| Agents           | MCP server exposing AF-specific tools to any MCP client                                                                                                                                                                                                                                                               |

## Component status

Whether each component on the cluster is running what is on `main`
([which ref each root deploys](deploy/README.md);
[![awaiting deployment][status-pending]](https://github.com/PurdueAF/purdue-af/releases)).

**Core** — ![platform][platform-version]

![af-users-graph][core-af-utils-af-users-graph]
![af-x509-secrets][core-jupyterhub-af-x509-secrets]
![database-backup][core-jupyterhub-database-backup]
![jupyterhub][core-jupyterhub-jupyterhub]
![jupyterhub-ssh][core-jupyterhub-jupyterhub-ssh]
![userlist-sync][core-jupyterhub-userlist-sync]
![af-monitoring][core-monitoring-af-monitoring]
![grafana][core-monitoring-grafana]
![prometheus][core-monitoring-prometheus]
![storage][core-storage]

**Experimental**

![pixi-global-sync][experimental-af-utils-pixi-global-sync]
![agentic-interface][experimental-agentic-interface]
![dask-gateway-k8s][experimental-dask-gateway-dask-gateway-k8s]
![dask-gateway-k8s-slurm][experimental-dask-gateway-dask-gateway-k8s-slurm]
![flyte][experimental-flyte]
![interlink-hammer][experimental-interlink-hammer]
![self-repair][experimental-self-repair]
![af-monitoring][experimental-monitoring-af-monitoring]
![alloy][experimental-monitoring-alloy]
![loki][experimental-monitoring-loki]
![pyroscope][experimental-monitoring-pyroscope]
![tempo][experimental-monitoring-tempo]
![servicex-shared][experimental-servicex]
![servicex][experimental-servicex-servicex]
![servicex-anvil][experimental-servicex-servicex-anvil]
![servicex-eos][experimental-servicex-servicex-eos]
![servicex-s3][experimental-servicex-servicex-s3]
![servicex-test][experimental-servicex-servicex-test]
![supersonic][experimental-sonic-supersonic]
![supersonic-af][experimental-sonic-supersonic-af]
![supersonic-interlink][experimental-sonic-supersonic-interlink]
![supersonic-pr][experimental-sonic-supersonic-pr]
![model-manager][experimental-sonic-model-manager]
![kuberay-operator][experimental-ray-operator]
![sonic-ray][experimental-ray-sonic-ray]
![storage][experimental-storage]

**Images** — which tag each one ships on: [RELEASING.md](RELEASING.md).

![purdue-af][image-purdue-af]
![agentic-interface][image-agentic-interface]
![af-pod-monitor][image-af-pod-monitor]
![af-node-monitor][image-af-node-monitor]
![self-repair][image-self-repair]
![supersonic-model-manager][image-supersonic-model-manager]
![interlink-slurm-plugin][image-interlink-slurm-plugin]

Reading the badges:

- `deployed` — no drift, the cluster has it
- `awaiting release` — validated, and only a release stands between it and
  the cluster
- `validating` — CI has not finished on those commits yet
- `failed CI` — do not release; the drift is broken
- the trailing number is how many commits it has moved since it was deployed
- a leading `X.Y.Z` is the deployed version, for images on a version stream

Recomputed hourly and after every CI run on `main` by
[`component-status.yml`](.github/workflows/component-status.yml); the badge
data lives on the [`status`](https://github.com/PurdueAF/purdue-af/tree/status)
branch, so keeping it current never touches `main`.

How a change reaches the cluster, version rules and rollback:
[RELEASING.md](RELEASING.md).

[core-af-utils-af-users-graph]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/core-af-utils-af-users-graph.json
[core-jupyterhub-af-x509-secrets]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/core-jupyterhub-af-x509-secrets.json
[core-jupyterhub-database-backup]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/core-jupyterhub-database-backup.json
[core-jupyterhub-jupyterhub]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/core-jupyterhub-jupyterhub.json
[core-jupyterhub-jupyterhub-ssh]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/core-jupyterhub-jupyterhub-ssh.json
[core-jupyterhub-userlist-sync]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/core-jupyterhub-userlist-sync.json
[core-monitoring-af-monitoring]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/core-monitoring-af-monitoring.json
[core-monitoring-grafana]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/core-monitoring-grafana.json
[core-monitoring-prometheus]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/core-monitoring-prometheus.json
[core-storage]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/core-storage.json
[experimental-af-utils-pixi-global-sync]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-af-utils-pixi-global-sync.json
[experimental-agentic-interface]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-agentic-interface.json
[experimental-dask-gateway-dask-gateway-k8s]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-dask-gateway-dask-gateway-k8s.json
[experimental-dask-gateway-dask-gateway-k8s-slurm]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-dask-gateway-dask-gateway-k8s-slurm.json
[experimental-flyte]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-flyte.json
[experimental-interlink-hammer]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-interlink-hammer.json
[experimental-self-repair]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-self-repair.json
[experimental-monitoring-af-monitoring]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-monitoring-af-monitoring.json
[experimental-monitoring-alloy]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-monitoring-alloy.json
[experimental-monitoring-loki]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-monitoring-loki.json
[experimental-monitoring-pyroscope]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-monitoring-pyroscope.json
[experimental-monitoring-tempo]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-monitoring-tempo.json
[experimental-servicex]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-servicex.json
[experimental-servicex-servicex]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-servicex-servicex.json
[experimental-servicex-servicex-anvil]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-servicex-servicex-anvil.json
[experimental-servicex-servicex-eos]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-servicex-servicex-eos.json
[experimental-servicex-servicex-s3]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-servicex-servicex-s3.json
[experimental-servicex-servicex-test]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-servicex-servicex-test.json
[experimental-sonic-model-manager]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-sonic-model-manager.json
[experimental-sonic-supersonic]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-sonic-supersonic.json
[experimental-sonic-supersonic-af]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-sonic-supersonic-af.json
[experimental-sonic-supersonic-interlink]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-sonic-supersonic-interlink.json
[experimental-sonic-supersonic-pr]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-sonic-supersonic-pr.json
[experimental-ray-operator]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-ray-operator.json
[experimental-ray-sonic-ray]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-ray-sonic-ray.json
[experimental-storage]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/experimental-storage.json
[image-purdue-af]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/image-purdue-af.json
[image-agentic-interface]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/image-agentic-interface.json
[image-af-pod-monitor]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/image-af-pod-monitor.json
[image-af-node-monitor]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/image-af-node-monitor.json
[image-self-repair]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/image-self-repair.json
[image-supersonic-model-manager]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/image-supersonic-model-manager.json
[image-interlink-slurm-plugin]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/image-interlink-slurm-plugin.json
[status-pending]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PurdueAF/purdue-af/status/badges/_pending.json
[platform-version]: https://img.shields.io/github/v/tag/PurdueAF/purdue-af?filter=2*&sort=semver&label=platform&color=blue
[af-image-version]: https://img.shields.io/badge/dynamic/yaml?url=https%3A%2F%2Fraw.githubusercontent.com%2FPurdueAF%2Fpurdue-af%2Fmain%2Fapps%2Fjupyterhub%2Fjupyterhub%2Fvalues.yaml&query=%24.singleuser.image.tag&label=AF%20image&color=blue
