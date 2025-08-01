# Interlink

Kubernetes-SLURM integration service that allows Kubernetes pods to be scheduled on SLURM clusters.

## Features

- **Virtual Kubelet** - Presents SLURM resources as Kubernetes nodes
- **SLURM Plugin** - Manages job submission and monitoring
- **Network Tunneling** - WebSocket tunnels for pod access
- **Volume Integration** - Access to CVMFS and shared storage

## Components

- **helmrelease.yaml** - Main Interlink deployment
- **values.yaml** - Configuration for Purdue AF environment
- **cache-pvc.yaml** - Persistent storage for Interlink data
- **test-pod.yaml** - Test pods for validation
- **test-pod-sonic.yaml** - Sonic-specific test pods

## Configuration

- Uses SLURM on Purdue RCAC infrastructure
- Integrates with CVMFS for software distribution
- Supports GPU workloads via SLURM
- Provides web-based access to SLURM jobs 