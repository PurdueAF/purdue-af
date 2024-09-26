User interface (wip)
===========================

Basic interface components
---------------------------
JupyterLab provides an interactive interface for general code development.
The screenshot below shows the main elements of the interface:
* Launcher - features buttons to create Python and ROOT C++ notebooks with different Conda environments, open terminals, create new text files, etc. Launcher window can be opened by clicking the `+` button.
* File browser - your home directory with symlinks to different storage volumes (Depot, CVMFS, /work/, etc. - learn more here).
* Terminal - standard Bash terminal, useful for any cases that require a command line interface, such as `voms-proxy-init`. You can also activate Conda environments here, run shell or Python scripts, use ROOT console, etc.
* File editors - simple IDE with syntax highlight for most common programming languages.

Extensions
-----------

Python code development
------------------------

* Jupyter notebooks
* Conda environments / kernels
* Dask

ROOT
-------
* C++ kernel - use Jupyter as command line
* PyROOT
* WIP: CUDA backend

HEP analysis frameworks
-------------------------
* Coffea
* PocketCoffea
* RooDataFrame

Scaling out
------------
* Slurm
* Dask
* CRAB

GPUs
------