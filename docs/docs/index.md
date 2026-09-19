# Purdue Analysis Facility

The Purdue Analysis Facility (Purdue AF) provides an interactive environment
for fast and scalable CMS physics analyses using dedicated computing resources at Purdue.

<div style="text-align: center" markdown>
[🚀 Login to Purdue Analysis Facility](https://cms.geddes.rcac.purdue.edu/hub){ target="_blank" }
</div>

Log in with a Purdue, CERN, or FNAL account — see
[Login methods](login-methods.md).

## What you get

* **A personal JupyterLab session** with the CPU cores and memory you
  [choose](scaling-out.md#session-resources), and optional
  [Nvidia A100 or T4 GPUs](gpus.md) — see [Getting started](getting-started.md).
* **A modern HEP software stack** managed via [Pixi environments](software.md),
  including `coffea`, `ROOT`, `RDataFrame`, and popular machine learning libraries
  such as `pytorch`, `tensorflow`, and `xgboost`.
* **Scalable computing** via [Dask Gateway](guide-dask-gateway.md) and, for
  Purdue users, [Slurm batch jobs](scaling-out.md).
* **Multiple data access methods** — [XRootD, XCache, Rucio](data-access.md) —
  and a variety of [private and shared storage volumes](storage.md).
* **Flexible access options**: web browser (JupyterLab or VS Code),
  [SSH](guide-ssh-access.md), [your local VSCode-based IDE](guide-ide-connection.md),
  or [any MCP-capable AI agent](guide-agentic-interface.md).

The software and functionality are regularly updated to provide state-of-the-art
tools and features for fast, efficient, collaborative HEP research.

## Where to start

| If you want to...                          | Go to...                                                |
| ------------------------------------------ | ------------------------------------------------------- |
| Create your first session                  | [Getting started](getting-started.md)                   |
| Learn the interface                        | [How to use Purdue AF](interface.md)                    |
| Understand where to store your files       | [Storage volumes](storage.md)                           |
| Set up an analysis environment             | [Pixi environments](guide-pixi.md)                      |
| Read CMS datasets                          | [Data access](data-access.md)                           |
| Scale your analysis to hundreds of cores   | [Scaling out](scaling-out.md)                           |
| Manage your session with an AI agent       | [Agentic interface](guide-agentic-interface.md)         |
| Fix a problem                              | [Troubleshooting](troubleshooting.md)                   |
| Ask a question                             | [Support](support.md)                                   |

<figure markdown="span">
  ![Purdue AF User Statistics](https://cms.geddes.rcac.purdue.edu/users-graph/purdue-af-registered-users.png){ width="900" }
</figure>
