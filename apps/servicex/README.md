# ServiceX

Data transformation service for high-energy physics data analysis.

## Features

- **Data Transformation** - Converts ROOT files to columnar formats
- **Code Generation** - Automatic code generation for data processing
- **Object Storage** - MinIO-based data storage
- **Message Queue** - RabbitMQ for job coordination
- **Database** - PostgreSQL for metadata management

## Components

- **helmrelease.yaml** - Main ServiceX deployment
- **values.yaml** - Purdue AF-specific configuration
- **kustomization.yaml** - Kustomize configuration

## Supported Formats

- **uproot** - ROOT file processing with uproot
- **uproot-raw** - Raw uproot processing with compression
- **python** - Python-based transformations
- **atlasr21/r22** - ATLAS-specific formats
- **cmssw** - CMS software framework

## Integration

- Connects to Purdue AF storage systems
- Integrates with monitoring stack
- Supports GPU acceleration
- Provides REST API for job submission 