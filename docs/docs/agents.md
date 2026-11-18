# AI agents in a session

Every Purdue AF session comes with the coding agents **Claude Code**, **Codex**
and **OpenCode** installed and already connected to the facility as you. This
page is about using them inside a session. To drive the facility from an agent
on your own machine, see
[AI agents on your machine](guide-agentic-interface.md); to ask a question
without a session, see [PurdueAF Agent on Mattermost](support.md#purdueaf-agent).

## Start an agent

| Where             | How                                                                                   |
| ----------------- | ------------------------------------------------------------------------------------- |
| Terminal          | Run `claude`, `codex` or `opencode`.                                                  |
| VS Code interface | Open the Claude Code or Codex extension. VS Code's built-in chat is turned off.       |
| JupyterLab        | Open the chat in the left sidebar. OpenCode answers; `@`-mention **Claude** or **Codex** for the others. |

The AF provides no model access: you sign each agent in with your own account
— Anthropic for Claude Code, OpenAI for Codex. OpenCode works without an
account on its free models, and takes your own key with `/connect`.

In JupyterLab the chat is [Jupyter AI](https://jupyter-ai.readthedocs.io), and
an agent there can also read and edit the notebook you have open. Notebooks
save themselves as you work and keep executing when your browser disconnects.

## What an agent knows here

Nothing to set up: an agent in a session starts with

* **the facility's rules** — which storage volume is for what and its quota,
  where environments may live, which volumes Dask, Ray and Slurm workers see.
  It knows, for example, that `pixi install` is refused under `/home` and that
  `/work` is invisible to Slurm jobs;
* **the facility's tools** — your session, storage use, Dask clusters, Ray
  jobs, logs, the facility's health and this documentation, with no token to
  handle. [What you can ask](guide-agentic-interface.md#what-you-can-ask) lists
  them.

So you can ask in plain language:

* "Create a Dask cluster with 20 workers on my project's environment and run
  `analysis.py` on it."
* "Why did my Dask workers die in the last hour?"
* "Set up a Pixi environment for this repository and make it a kernel."

## An agent acts as you

An agent in your session is a process of yours: it reads and writes the storage
you can, holds your credentials (grid proxy, Kerberos ticket, session token),
and reaches Slurm, Dask Gateway and Ray as you do.

* You are responsible for what an agent does in your session, as for any
  command you run yourself.
* An agent may run unattended. Your limits are its limits: the CPU and memory
  of the session, your [storage quotas](storage.md#quotas), and the limits on
  [Dask Gateway clusters](guide-dask-gateway.md) and [GPUs](gpus.md).
* An agent sends what it reads to the model provider you signed it in with.

The agents know the facility's storage rules. Two of them the filesystem does
not enforce, so they are worth knowing yourself: nothing is ever created at the
top level of `/depot/cms/`, and under `/work/projects/` an agent stays in the
project directory you name.
