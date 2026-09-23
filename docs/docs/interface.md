# How to use Purdue AF

## User interfaces

Purdue AF provides several user interfaces for analysis development:

* **JupyterLab** — interactive web interface for notebooks, terminals, file
  browsing, and more. This is the default interface when starting a session.
* **Web-based Visual Studio Code (code-server)** — to open it, either select the 
  VSCode interface when creating a session, or click "Open VSCode" button at top
  right of the JupyterLab interface.
* [Connection from local VSCode-based IDEs](guide-ide-connection.md) (VSCode, Cursor, etc.)
* [SSH connection from a local terminal](guide-ssh-access.md)
* [Agentic interface (MCP server)](guide-agentic-interface.md) — manage your AF
  session and Dask clusters from any MCP-capable AI agent. Claude Code and Codex
  come pre-installed and pre-connected in the session (terminal and VS Code
  extensions), and can be
  [used as a chat panel inside JupyterLab](guide-agentic-interface.md#ai-in-jupyterlab); agents on your own machine (Cursor, etc.) connect with a token.

## Python code development

JupyterLab is especially well suited for developing analysis workflows in Python.

* **Jupyter Notebooks** allow you to write analysis code as a sequence of code and
  text cells, which can be executed in arbitrary order. In many cases, a single
  Jupyter Notebook can accommodate a full analysis from data access to producing
  final plots.

    Jupyter Notebooks support a wide range of plugins and widgets, which allows for
    a more interactive experience compared to plain Python scripts.

* To execute the code in a Jupyter Notebook, you always need to specify a **kernel**.
  At Purdue AF, Jupyter kernels are derived from Pixi or Conda environments —
  read more in [Software stacks](software.md).
* We provide a curated ["global" Pixi environment](software.md), which should work
  for most applications, unless your code relies on a very specific package version.
* Analysis code written in Python can be accelerated via parallelization. We recommend
  using [Dask](guide-dask.md) for parallelization and distributed computing.
  For scaling out to multiple computing nodes, use [Dask Gateway](guide-dask-gateway.md).

## ROOT

[ROOT](https://root.cern) is a software package developed by CERN and widely used in
high energy physics for histogramming, fitting, and statistical analysis.

* ROOT is installed in the [global Pixi environment](software.md#the-global-pixi-environment),
  not in the bare session. In a terminal, activate an environment that contains
  ROOT (for example, `pixi shell` in `/work/pixi/global/`) and launch the ROOT
  console with `root -l`. Note that it is not possible to display canvases or
  open `TBrowser`, since the JupyterLab interface does not support X11 forwarding.
* Alternatively, you can turn a Jupyter Notebook into a ROOT console by selecting
  the [ROOT C++ kernel](software.md#root-c-kernel). Similarly to Python notebooks,
  you can add text cells and execute cells in arbitrary order, and display ROOT
  plots with the `TCanvas::Draw` method.
* In Python, ROOT functionality is accessible via the
  [PyROOT](https://root.cern/manual/python/) package, available in the global
  environment. In a Jupyter Notebook, you can display ROOT plots using the
  `TCanvas::Draw` method.
* To run RooFit fits on a GPU, see [Accelerating RooFit with GPUs](guide-roofit-cuda.md).

## HEP analysis frameworks

We aim to support a wide range of modern HEP analysis tools. Below are a few
examples of frameworks which have been shown to perform well at Purdue AF:

* [Coffea](https://coffea-hep.readthedocs.io/) is a popular Python package
  for efficient columnar particle physics analyses. Coffea implements all common
  tools used in modern HEP analyses, and has a large and active support community.

    Coffea is included in the global Pixi environment at `/work/pixi/global/`.

* [PocketCoffea](https://pocketcoffea.readthedocs.io/en/stable/) is a slim declarative
  framework built on top of Coffea. It allows you to define an analysis with a few
  configuration files. A PocketCoffea analysis can be executed in a distributed way
  using the
  [dask@purdue-af executor](https://pocketcoffea.readthedocs.io/en/stable/running.html#executors-availability),
  which is based on [Dask Gateway](guide-dask-gateway.md).

* [RDataFrame](https://root.cern.ch/doc/master/group__tutorial__dataframe.html) is
  another common HEP analysis framework based on ROOT. An RDataFrame analysis can
  be written in either C++ or Python. Purdue AF supports RDataFrame in any Pixi or
  Conda environment where ROOT is installed.

## Scaling out

When your analysis outgrows the resources of a single session, several options are
available — see [Scaling out](scaling-out.md) for a detailed comparison:

* **[Dask](guide-dask.md)** — parallelize any Python code over local cores, or
  scale out via [Dask Gateway](guide-dask-gateway.md).
* **Slurm** — batch submission to Purdue computing clusters (Purdue users only).
* **CRAB** — submission of CMSSW jobs to the Worldwide LHC Computing Grid.

## GPUs

At Purdue AF, you can start a session with a GPU by selecting it at the resource
selection step — see [GPU access at Purdue AF](gpus.md).
