# Purdue Analysis Facility

The Purdue Analysis Facility (Purdue AF) provides an interactive environment
for fast and scalable CMS physics analyses using dedicated computing resources at Purdue.

<div style="text-align: center" markdown>
[🚀 Login to Purdue Analysis Facility](https://cms.geddes.rcac.purdue.edu/hub){ target="_blank" }
</div>

!!! note "Supported login credentials"

    * Purdue University account
    * CERN account (CMS users only)
    * FNAL account

## What you get

* **A personal JupyterLab session** with up to 128 CPU cores, 128 GB RAM, and
  optional Nvidia A100 GPUs — see [Getting started](getting-started.md).
* **A modern HEP software stack** managed via [Pixi environments](software.md),
  including `coffea`, `ROOT`, `RDataFrame`, and popular machine learning libraries
  such as `pytorch`, `tensorflow`, and `xgboost`.
* **Scalable computing** via [Dask Gateway](guide-dask-gateway.md) (up to 200 cores
  and 1.2 TB RAM per user) and, for Purdue users, [Slurm batch jobs](scaling-out.md).
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

!!! note "Join the Purdue AF support channel on CERN Mattermost"

    [https://mattermost.web.cern.ch/cms-exp/channels/purdue-analysis-facility](https://mattermost.web.cern.ch/cms-exp/channels/purdue-analysis-facility)
    (CERN login required)

<figure markdown="span">
  ![Purdue AF User Statistics](https://cms.geddes.rcac.purdue.edu/users-graph/purdue-af-registered-users.png){ width="900" }
</figure>
