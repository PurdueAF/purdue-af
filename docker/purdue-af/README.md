# purdue-af — the Analysis Facility singleuser image

The image every AF session runs, built on the CUDA devel base named in the
[`Dockerfile`](Dockerfile)'s `FROM` line.

Contents beyond the CUDA base: the pixi `base-env` (`pixi/base/` — JupyterLab,
code-server, kernels), OSG grid clients (`voms-clients-cpp`, gfal2, xrootd),
the Slurm client with Hammer configs, CVMFS/XRootD site config, and the coding
agents.

## Files

| Path | What it is |
| --- | --- |
| `Dockerfile` | the build; the `smoke` stage holds the build-time checks |
| `jupyter/` | `start.sh` and the hook runner, server config, healthcheck |
| `scripts/` | the `before-notebook.d` hooks, `af-as-user.sh`, `managed-block.py` |
| `agents/platform-context.md` | the facility context every in-session agent reads |
| `pixi-wrapper` | the `pixi` on a session's PATH |
| `configs/`, `osg/`, `xml/` | CERN krb5/CA defaults, OSG RPMs, CMS site config |
| `code-server/` | the interface-controls extension |

## Coding agents

The `claude`, `codex` and `opencode` CLIs, plus the `claude-agent-acp` and
`codex-acp` adapters that jupyter-ai's personas need, are installed into
`/opt/npm-global` at the versions pinned by the Dockerfile `ARG`s. Their
code-server extensions are installed at startup by `config-extensions.sh`.

`config-agents.sh` wires them up on every start: it registers the AF MCP
server as `purdue-af-agentic-interface` (the name `.mcp.json` uses) at its
in-cluster address, installs the bundled skill into `~/.claude/skills/`, and
has `managed-block.py` write [`platform-context.md`](agents/platform-context.md)
into `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md`. The skill is
`.claude/skills/purdue-af-agentic-interface/SKILL.md`, shipped as is. The image ships no model
credentials; users sign the agents in themselves.

## Build and publication

Built by [`ci.yml`](../../.github/workflows/ci.yml) from the inputs listed in
[`image-inputs.sh`](../../.github/workflows/image-inputs.sh). Which sessions
pull which tag, release and rollback: [RELEASING.md](../../RELEASING.md);
registries: [REGISTRY.md](../REGISTRY.md).

## Release checklist

Before running **Release image**, verify on a test session: GPU visibility
(`nvidia-smi`, torch/TF), that a 0-GPU session sees no GPUs, `eos-connect.sh`
(kinit against CERN.CH), grid workflows (`voms-proxy-init`, gfal2, xrootd), and
Slurm (`sbatch`/`squeue`).
