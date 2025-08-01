# Jupyter

JupyterHub deployments for interactive computing environments.

## Components

- **jupyterhub/** - Main JupyterHub instance for general use
- **jupyterhub-ssh/** - JupyterHub with SSH access capabilities
- **jupyterhub-database-backup/** - Automated database backup service

## Features

- Multi-user Jupyter notebook environments
- Custom kernels for CMS analysis
- Integration with Purdue AF storage and compute resources
- SSH access for advanced users
- Automated backup and recovery

## Configuration

- Uses Helm charts for deployment
- Custom spawner for resource management
- Integration with Purdue AF authentication
- Support for GPU-enabled notebooks
- Database persistence and backup 