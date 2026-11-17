# purdue-af — the Analysis Facility singleuser image

The image every AF session runs, built on the CUDA devel base named in the
[`Dockerfile`](Dockerfile)'s `FROM` line.

Contents beyond the CUDA base: the pixi `base-env` (`pixi/base/` — JupyterLab,
code-server, kernels), OSG grid clients (`voms-clients-cpp`, gfal2, xrootd),
the Slurm client with Hammer configs, and the coding agents.

## Files

| Path | What it is |
| --- | --- |
| `Dockerfile` | the build; the `smoke` stage holds the build-time checks |
| `jupyter/` | `start.sh`, which runs the hooks and starts the server as the user; server config |
| `scripts/` | the `before-notebook.d` hooks, `af-as-user.sh`, `managed-block.py` |
| `agents/platform-context.md` | the facility context every in-session agent reads |
| `agents/claude-sandbox.json` | Claude Code's managed sandbox settings |
| `pixi-wrapper` | the `pixi` on a session's PATH |
| `configs/`, `osg/` | CERN krb5/CA defaults, OSG RPMs |
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

Claude Code's sandbox is off until a user turns it on, and the image holds what
it needs: `socat`, a link at `/opt/npm-global/bwrap` to the bubblewrap Codex
vendors (the build fails when Codex ships none), and
`agents/claude-sandbox.json`, installed as
`/etc/claude-code/managed-settings.d/sandbox.json`, which names that binary and
has the sandbox reuse the pod's `/proc`. What each agent's sandbox confines:
[the user guide](../../docs/docs/guide-agentic-interface.md#sandbox).

## Build and publication

Built by [`ci.yml`](../../.github/workflows/ci.yml) from the inputs listed in
[`image-inputs.sh`](../../.github/workflows/image-inputs.sh). Which sessions
pull which tag, release and rollback: [RELEASING.md](../../RELEASING.md);
registries: [REGISTRY.md](../REGISTRY.md).

## Release checklist

Bump `OS_UPGRADE_ID` in the `Dockerfile` in a PR ahead of each image release, so
OS updates soak on `:pre-release` first: the build cache reuses `dnf upgrade`
until that ID changes.

Before running **Release image**, verify on a test session: GPU visibility
(`nvidia-smi`, and `/work/pixi/global/.pixi/envs/default/bin/python
pixi/global/check-gpu.py` from a checkout), that a 0-GPU session sees no GPUs,
`eos-connect.sh` (kinit against CERN.CH), grid workflows (`voms-proxy-init`,
gfal2, xrootd), and Slurm (`sbatch`/`squeue`).
