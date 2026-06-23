# Software stacks

## Conda environments and Jupyter kernels

The Purdue Analysis Facility provides multiple Jupyter kernels with pre-installed
analysis software. Users can also create their own kernels from scratch
or from the existing kernels using the following instructions:
[Creating Conda environments and Jupyter kernels](guide-conda.md).

The pre-installed kernels are listed below. The versions of the packages
in these kernels will be occasionally upgraded.

### 1. Python3 kernel (default)

This kernel is based on Python 3.10 and designed for typical pythonic HEP analysis
workflows.

* In new Jupyter notebooks, this kernel will be automatically selected as default.
* In Terminal, it can be activated as follows:

    ```shell
    conda activate /depot/cms/kernels/python3
    ```

The environment is built from the following YAML file:

??? note "python3-env.yaml"

    ```yaml
    name: python3
    channels:
      - conda-forge
    dependencies:
      # CONDA PACKAGES
      - python=3.10.10

      # Scientific computing and data analysis
      - awkward=1.10.3
      - awkward-cpp=15
      - lmfit
      - numba
      - numpy=1.24.4
      - pandas=1.5.3
      - scipy=1.10
      - scikit-learn=1.5.2
      - uncertainties
      - h5py=3.8.0

      # JuliaCall
      - pyjuliacall

      # High energy physics tools
      - coffea=0.7.21
      - hist
      # - lhapdf - installed manually (see PATHs)
      - pyhf
      - root=6.32.2
      - uproot=4.3.7
      - vector=1.4.2
      - cabinetry
      - hepdata-lib

      # Plotting
      - bokeh=3.1.0
      - matplotlib
      - mplhep
      - plotly
      # - texlive-core
      # - tectonic
      - py-spy
      - graphviz
      - python-graphviz
      - pydot
      - ipympl

      # Machine learning
      - mup
      # - tensorflow & keras - see pip packages
      # - torch & torch_geometric - see pip packages
      - xgboost
      - optuna
      - scikit-optimize

      # Distributed computing
      - dask
      - dask-gateway
      - dask-jobqueue
      - distributed
      # - dask=2023.3.2
      # - dask-gateway=2023.9.0
      # - dask-jobqueue=0.8.1
      # - distributed=2023.3.2
      - dask-ml
      - dask-xgboost
      - dask-memusage
      - dask-histogram

      # C++ libraries
      - boost
      - eigen
      - gsl
      - pcre
      - tbb
      - vdt
      - gcc_linux-64
      - gxx_linux-64
      - libgcc-ng
      - libstdcxx-ng

      # Other tools
      - ca-certificates
      - certifi
      - click=8.1.3
      - gpustat
      - ipykernel
      - ipywidgets
      - mamba
      - openssl
      - pip
      - pyarrow
      - pytest
      - tqdm
      - xrootd
      - yaml
      - pycurl
      - mimesis=8
      - setuptools=67

      # PIP PACKAGES
      - pip:
          - rucio-clients
          - scalpl
          - tensorflow[and-cuda]==2.15.1
          - tensorflow-probability==0.23
          - pyarrow==10.0.1
          - uproot==4.3.7
          - awkward==1.10.3
          - vector==1.4.2
          - tf-keras
          - qkeras
          - torch==2.0.1
          - torch-geometric==2.3.1
          - git+https://github.com/mattbellis/particle_physics_simplified.git
          - git+https://github.com/mattbellis/h5hep.git
          - PhyPraKit
          - dbs3-client
          - correctionlib==2.5.0
          - cmsstyle
          - xgbfir
          - servicex==3.1.1

        # must run this command: pip uninstall nvidia-cudnn-cu11

    variables:
      PATH: /depot/cms/kernels/python3/bin:/usr/sue/bin:/etc/jupyter/dask:/opt/conda/condabin:/opt/conda/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/depot/cms/purdue-af/lhapdf/bin
      LD_LIBRARY_PATH: :/depot/cms/purdue-af/lhapdf/lib:/opt/conda/lib/
      PYTHONPATH: :/depot/cms/purdue-af/lhapdf/lib/python3.10/site-packages

    prefix: /depot/cms/kernels/python3
    ```

### 2. `coffea_latest` kernel

This kernel features the latest version of [Coffea](https://coffeateam.github.io/coffea/) package,
which is regularly updated. In contrtast, the Coffea version in the default
environment is fixed to `0.7.21`.

!!! note

    If you want to use the `coffea_latest` environment but missing some packages,
    please [contact Purdue AF admins](doc-support.md) and we will install them
    for you.

!!! note

    - Last updated: May 28, 2025
    - Coffea version: `2025.3.0`

* In new Jupyter notebooks, this kernel will appear as `Python[conda env:coffea_latest]`.
* In Terminal, it can be activated as follows:

    ```shell
    conda activate /depot/cms/kernels/coffea_latest
    ```

The environment is built from the following YAML file:

??? note "coffea_latest.yaml"

    ```yaml
    name: coffea_latest
    channels:
      - conda-forge
    dependencies:
      - python
      - pip

      # HEP
      - coffea==2025.3.0
      - awkward
      - uproot
      - hist
      - mplhep
      - uncertainties
      - lmfit
      - correctionlib==2.6.4
      - hepconvert

      # ROOT
      - root==6.32.02
      - root_base==6.32.02
      - boost
      - eigen
      - gsl
      - pcre
      - tbb
      - vdt

      # ML
      - xgboost

      # Tools
      - dask
      - dask-gateway
      - dask-awkward==2025.5.0
      - prometheus_client
      - xrootd
      - ipywidgets
      - ipykernel
      - omegaconf
      - seaborn

      - libnvjitlink-dev

      - pip:
          - rucio-clients==33.3.0
          - fsspec-xrootd
          - torch
          - cmsstyle
          - scikit-hep-testdata
          - graphviz

    variables:
      LD_LIBRARY_PATH: /depot/cms/kernels/coffea_latest/lib/python3.11/site-packages/nvidia/nvjitlink/lib/:/usr/local/cuda-12.2/lib64:/depot/cms/purdue-af/combine/HiggsAnalysis/CombinedLimit/build/lib:/depot/cms/purdue-af/roofit-batchcompute/build/
      PYTHONPATH: /depot/cms/kernels/coffea_latest/bin
    ```

### 3. ROOT C++ kernel

This kernel provides an interactive interface to the ROOT command line,
allowing to execute ROOT macros and produce plots inside Jupyter notebooks.

!!! note "See also"

    [ROOT C++ notebook demo](demos/root-cpp.md)

## Software stacks from CVMFS

It is possible to load centrally distributed CERN software stacks for CVMFS, such as
LCG Releases and Apptainer/Singularity images.

!!! warning

    Methods described in this section will work only in Terminal, it is not currently
    possible to use the LCG stacks and Apptainer images to launch custom Jupyter kernels.

### 1. LCG Releases

The [LCG Documentation](https://lcgdocs.web.cern.ch/lcgdocs/lcgreleases/introduction/) page
contains information about software releases avaiable for loading via CVMFS.
Loading remote software is possible either via LCG **views** for entire software stacks,
or via LCG **releases** for specific packages.

### 2. Apptainer (Singularity) images

Another method of loading centrally maintained software is via Apptainer (formerly Singularity)
images distributed via CVMFS. This can be useful if you need to run a code that requires a
specific operating system (Purdue AF is based on AlmaLinux8).

Example of loading an Apptainer image based on EL7:

```
$ /cvmfs/cms.cern.ch/common/cmssw-el7
Singularity>
```

!!! warning

    Your Analysis Facility session already runs in a Docker container. Launching Apptainer
    inside the AF session leads to a "container-in-container" setup, which is not guaranteed
    to always work as intended.

    We do not recommend using Apptainer at Purdue AF, unless it is absolutely needed.
