# Purdue AF Applications

This directory contains all the Kubernetes applications that make up the Purdue Analysis Facility (AF). Each subdirectory represents a different component that gets deployed via Flux CD.

## Components

- **cvmfs-pvc.yaml** - Persistent Volume Claim for CVMFS storage
- **dask-gateway/** - Distributed computing clusters for data analysis
- **git-and-helm-repos/** - Git repositories and Helm chart repositories
- **interlink/** - Kubernetes-SLURM integration for job scheduling
- **jupyter/** - JupyterHub instances for interactive computing
- **kaniko-build-jobs/** - Container image building jobs
- **monitoring/** - Prometheus, Grafana, and custom monitoring
- **purdue-af-utils/** - Utility services for user management and maintenance
- **servicex/** - ServiceX data transformation service
- **sonic/** - NVIDIA Triton inference server for ML models

All components are managed by Flux CD and use Kustomize for configuration management. 