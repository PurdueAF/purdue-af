# Software stacks

Analysis software at Purdue AF can come from several sources, listed here from most
to least recommended:

1. **Pixi environments** — the main way to manage analysis software since 2026.
2. **Conda environments** — still supported, but being phased out in favor of Pixi.
3. **LCG stacks** distributed via CVMFS — useful for special cases such as
   CUDA-enabled ROOT.
4. **Apptainer/Singularity images** via CVMFS — a last resort for software that
   requires a different operating system.

## Pixi environments

[Pixi](https://pixi.sh/) is a modern package manager and a successor of Conda/Mamba.
It is significantly faster than Conda, resolves Conda and PyPI dependencies together, and enforces reproducibility via lock files.

Unlike Conda environments, Pixi environments are meant to be **project-specific**:
the environment definition (`pixi.toml`) and the environment itself live in the
project directory, next to your analysis code. A detailed guide on how to start
using Pixi is available here: [Pixi guide](guide-pixi.md).

### The global Pixi environment

In addition to project-specific environments, we provide a **global Pixi
environment** at `/work/pixi/global/`, which contains all common HEP analysis
packages and ML libraries. It is a good starting point for new projects and for
code that is not part of any Pixi project.

??? note "List of packages in the global environment (pixi.toml configuration)"

    The canonical definition lives in
    [`pixi/global/pixi.toml`](https://github.com/PurdueAF/purdue-af/blob/main/pixi/global/pixi.toml)
    in this repository:

    ```toml
    --8<-- "pixi/global/pixi.toml"
    ```

If a package that you consider common is missing from the global environment,
[let us know](support.md) — we update it regularly.

## Jupyter kernels

We provide multiple types of Jupyter kernels to execute analysis code in notebooks.

### Pixi kernels

There is no one-to-one mapping between Pixi environments and Jupyter kernels.
Instead, we provide two special Pixi kernels:

- **"global" kernel** — always uses the global environment at `/work/pixi/global/`.
- **"project-aware" kernel** — automatically discovers the environment local to the
  directory where the notebook is located. If no local environment is found, the
  kernel falls back to the global environment.

!!! note

    In order for a Pixi environment to be discoverable by the "project-aware"
    kernel, it must have the `ipykernel` package installed, and be stored in a readable directory.

### Conda kernels

Conda environments are automatically discovered by Jupyter if they have the
`ipykernel` package installed — see
[Creating Conda environments and Jupyter kernels](guide-conda.md).

### ROOT C++ kernel

This kernel provides an interactive interface to the ROOT command line, allowing
you to execute ROOT macros and produce plots inside Jupyter notebooks.

!!! note "See also"

    [ROOT C++ notebook demo](https://github.com/PurdueAF/purdue-af-demos/blob/master/root-cpp.ipynb)

### LCG kernels

These kernels are based on LCG "views" loaded via CVMFS:

- **`LCG_106b`** — the standard LCG software stack;
- **`LCG_106b_cuda`** — contains the CUDA-enabled ROOT build and is suitable for
  [running RooFit on GPUs](guide-roofit-cuda.md).

## Combine

[Combine](https://cms-analysis.github.io/HiggsAnalysis-CombinedLimit/v10.3.3/) is
a RooStats / RooFit based software framework widely used in LHC experiments for
statistical analysis of experimental data.

At Purdue AF, Combine can be used either in a CMSSW environment or in standalone
mode in Pixi or Conda environments. We recommend the standalone mode — the
`cms-combine` package is already available in the global Pixi environment, and can
be added to your own environment with a single command.

See [Using Combine at Purdue AF](guide-combine.md) for details.

## CMSSW

CMSSW releases are available via CVMFS in the usual way:

```shell
source /cvmfs/cms.cern.ch/cmsset_default.sh
cmsrel CMSSW_13_0_13
cd CMSSW_13_0_13/src
cmsenv
```

Note that Purdue AF is based on an EL8 system (RHEL8-compatible, `el8`/`slc8`
architectures), so CMSSW releases built for other
architectures (e.g. `slc7`) must be run inside an Apptainer container such as
`cmssw-el7` (see below, and the [MC generation guide](guide-mc-gen.md) for a
worked example).

## Apptainer / Singularity images

In rare cases when you need to run code that requires a specific operating system,
you can load Apptainer/Singularity images via CVMFS.

Example of loading an Apptainer image based on EL7:

```
$ /cvmfs/cms.cern.ch/common/cmssw-el7
Singularity>
```

!!! warning

    Your Analysis Facility session already runs in a Docker container. Launching
    Apptainer inside the AF session leads to a "container-in-container" setup, which
    is not guaranteed to always work as intended.

    We do not recommend using Apptainer at Purdue AF unless it is absolutely needed.
