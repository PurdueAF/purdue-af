# Kaniko Build Jobs

Container image building jobs using Kaniko for secure, unprivileged builds.

## Components

- **build-af.yaml** - Builds Purdue AF base images
- **build-dask.yaml** - Builds Dask Gateway images
- **build-interlink.yaml** - Builds Interlink plugin images
- **secret.yaml** - Registry authentication secrets

## Features

- Secure container builds without Docker daemon
- Integration with Purdue registry
- Automated image updates
- Support for multi-architecture builds

## Usage

These jobs are triggered by Flux CD to rebuild container images when source code changes. Kaniko provides secure, reproducible builds within Kubernetes pods. 