# Dask Gateway

Distributed computing clusters for data analysis using Dask Gateway.

## Components

- **dask-gateway-k8s/** - Standard Kubernetes-based Dask clusters
- **dask-gateway-k8s-interlink/** - Dask clusters integrated with Interlink for SLURM access
- **dask-gateway-k8s-slurm/** - Dask clusters with direct SLURM backend

## Features

- Multi-backend support (Kubernetes, SLURM, Interlink)
- Resource management and scheduling
- Integration with Purdue AF storage (CVMFS, EOS, depot)
- GPU support for compute-intensive workloads
- Web-based cluster management interface

Each deployment uses Helm charts with custom values for Purdue AF infrastructure. 