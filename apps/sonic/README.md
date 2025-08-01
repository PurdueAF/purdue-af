# Sonic

NVIDIA Triton inference server for machine learning models in high-energy physics.

## Components

- **supersonic/** - Standard ML inference service
- **supersonic-dynamic/** - Dynamic model loading and scaling
- **supersonic-interlink/** - Interlink-integrated inference service

## Features

- **NVIDIA Triton** - High-performance inference server
- **GPU Acceleration** - Optimized for GPU workloads
- **Model Repository** - CVMFS-based model distribution
- **Auto-scaling** - KEDA-based horizontal scaling
- **Load Balancing** - Envoy proxy for traffic management
- **Monitoring** - Prometheus metrics and Grafana dashboards

## Supported Models

- CMS b-tagging models
- DeepTau identification
- MET subtraction
- Egamma photon classification

## Configuration

- Uses Purdue AF GPU infrastructure
- Integrates with CVMFS for model access
- Supports dynamic model loading
- Provides REST and gRPC APIs
- Includes tracing and observability 