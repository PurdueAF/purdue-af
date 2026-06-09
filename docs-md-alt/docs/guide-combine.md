# Using Combine at Purdue AF

[Combine](https://cms-analysis.github.io/HiggsAnalysis-CombinedLimit/v10.3.3/) is
a RooStats / RooFit based software framework widely used in LHC experiments for
statistical analysis of experimental data.

Combine can be used either in a CMSSW environment or in standalone mode in Pixi or
Conda environments. At Purdue AF, we recommend using **standalone mode**, because
some CMSSW releases are not compatible with the operating system, and loading other
operating systems — while possible via Apptainer — can cause unexpected issues.

The fastest way to try Combine is the [global Pixi environment](software.md), which
already includes the `cms-combine` package:

```shell
$ pixi shell --manifest-path /work/pixi/global/pixi.toml
$ combine --help
```

## Combine in Pixi environments

### Install from conda-forge (recommended)

You can easily install Combine from the `conda-forge` distribution into your own
Pixi environment by adding the `cms-combine` package to your Pixi project. Run the
following command in the project directory (which contains the `pixi.toml` file):

```shell
pixi add cms-combine==10.4.2
```

OR add the package explicitly to the `[dependencies]` section of the `pixi.toml` file:

```toml
[dependencies]
cms-combine = "==10.4.2"
```

### Install from source

You can also install Combine from source by adding a "task" (custom command) to
your Pixi project. This can be useful if you want to test features in an unreleased
version of Combine.

All you need to do is copy the following code block into the `[tasks]` section of
the `pixi.toml` file:

```toml
[tasks]
install_combine = """
sh -c '
set -e

# Delete existing Combine installation, if present
rm -rf HiggsAnalysis/CombinedLimit

# Clone latest Combine version from GitHub
git clone https://github.com/cms-analysis/HiggsAnalysis-CombinedLimit.git HiggsAnalysis/CombinedLimit
cd HiggsAnalysis/CombinedLimit

# Get the Python major.minor version from the active pixi env
PY_VER=$(python - << \"PY\"
import sys
print(f\"{sys.version_info.major}.{sys.version_info.minor}\")
PY
)

# Install Combine into the environment
cmake -S . -B build \
-DCMAKE_INSTALL_PREFIX=\"$CONDA_PREFIX\" \
-DCMAKE_INSTALL_PYTHONDIR=\"lib/python${PY_VER}/site-packages\" \
-DUSE_VDT=OFF
cmake --build build -j\"$(nproc --ignore=2)\"
cmake --install build

cd -
'
"""
```

Then, once your environment is built, you can install Combine by running:

```shell
pixi run install_combine
```

!!! warning

    For this to work, your environment must have the following packages installed:
    `root=6.34`, `gsl`, `boost-cpp`, `vdt`, `eigen`, `tbb`, `cmake`, `ninja`.

## Combine in pre-installed Conda environments

Standalone Combine is pre-installed in the two centrally managed Conda environments —
`/depot/cms/kernels/python3` and `/depot/cms/kernels/coffea_latest`. It is enough
to activate either of these environments to use Combine:

```shell
$ conda activate /depot/cms/kernels/python3
(/depot/cms/kernels/python3) $ combine -M Significance -d datacard.txt
<<< Combine >>>
<<< v10.3.3 >>>
```

## Combine in custom Conda environments

If you want to use Combine in your own Conda environment, you can similarly install
it from conda-forge by adding the `combine` package to the `environment.yaml` file.

To install Combine from source into a Conda environment, follow the instructions
below:

```bash
git clone https://github.com/cms-analysis/HiggsAnalysis-CombinedLimit.git HiggsAnalysis/CombinedLimit
cd HiggsAnalysis/CombinedLimit

# configure conda-forge as the preferred channel
conda config --set channel_priority strict
conda config --add channels conda-forge

# activate the environment
conda activate <your-conda-environment>

# install Combine's dependencies:
conda install root=6.34 gsl boost-cpp vdt eigen tbb cmake ninja
# NOTE: if your environment is managed using an environment.yaml file,
# add these dependencies to it and rebuild the environment, instead of installing them here.

# configure and build with CMake
cmake -S . -B build -DCMAKE_INSTALL_PREFIX=$CONDA_PREFIX -DCMAKE_INSTALL_PYTHONDIR=lib/python3.12/site-packages -DUSE_VDT=OFF
cmake --build build -j$(nproc --ignore=2)
cmake --install build
```

After installation, to use Combine in a new Terminal you only need to activate the
environment.
