# Purdue Analysis Facility

The Purdue Analysis Facility (Purdue AF) is an interactive environment for fast
and scalable CMS physics analyses on dedicated computing resources at Purdue.
You work in a personal JupyterLab or VS Code session, with CMS data, a
maintained HEP software stack, GPUs, and Dask and Ray clusters within reach.

<div style="text-align: center" markdown>
[🚀 Login to Purdue Analysis Facility](https://cms.geddes.rcac.purdue.edu/hub){ target="_blank" }
</div>

Log in with a Purdue, CERN, or FNAL account — see
[Login methods](login-methods.md).

<div class="grid cards" markdown>

-   **[Get started](getting-started.md)**

    ---

    Create your first session and set up GitHub and Grid access, in about 15
    minutes. Then [learn the interfaces](interface.md), or connect over
    [SSH](guide-ssh-access.md) or from [your own IDE](guide-ide-connection.md).

-   **[AI agents](agents.md)**

    ---

    Claude Code, Codex and OpenCode run in every session and already know the
    facility. You can also [connect an agent on your own machine](guide-agentic-interface.md),
    or [ask PurdueAF Agent on Mattermost](support.md#purdueaf-agent).

-   **[Scale out](scaling-out.md)**

    ---

    Run an analysis on hundreds of cores with
    [Dask Gateway](guide-dask-gateway.md), or submit Slurm and CRAB jobs.

-   **[Train on GPUs](gpus.md)**

    ---

    Start a session with an A100 or T4 GPU, or
    [train on several GPUs with Ray](guide-ray-train.md) from a notebook.

-   **[Software](software.md)**

    ---

    Use the shared Pixi environment with `coffea`, `ROOT` and `pytorch`, or
    [build your own](guide-pixi.md).

-   **[Data and storage](storage.md)**

    ---

    Know which volume to use for what, and
    [read CMS datasets](data-access.md) over XRootD, XCache and Rucio.

</div>

Something not working? See [Troubleshooting](troubleshooting.md) or
[ask for help](support.md).

<figure markdown="span">
  ![Purdue AF User Statistics](https://cms.geddes.rcac.purdue.edu/users-graph/purdue-af-registered-users.png){ width="900" }
</figure>
