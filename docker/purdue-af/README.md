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
| `agents/codex-config.toml` | Codex's system config, `/etc/codex/config.toml` |
| `agents/opencode.json` | the config layer `OPENCODE_CONFIG` names |
| `pixi-wrapper` | the `pixi` on a session's PATH |
| `configs/`, `osg/` | CERN krb5/CA defaults, OSG RPMs |
| `code-server/` | the interface-controls extension |

## Coding agents

The `claude`, `codex` and `opencode` CLIs, plus the `claude-agent-acp` and
`codex-acp` adapters that jupyter-ai's personas need, are installed into
`/opt/npm-global` at the versions pinned by the Dockerfile `ARG`s. Their
code-server extensions are installed at startup by `config-extensions.sh`.

Each agent gets the AF MCP server (`purdue-af-agentic-interface`, the name
`.mcp.json` uses, at its in-cluster address), the skill
(`.claude/skills/purdue-af-agentic-interface/SKILL.md`, shipped as is) and
[`platform-context.md`](agents/platform-context.md), from image files wherever
the agent reads a system-level one:

| Agent | MCP server | Skill | Platform context |
| --- | --- | --- | --- |
| Claude Code | `~/.claude.json`, by `claude mcp add` | `/etc/claude-code/.claude/skills/` | `/etc/claude-code/CLAUDE.md` |
| Codex | `/etc/codex/config.toml` | `/etc/codex/skills/` | `~/.codex/AGENTS.md`, by `managed-block.py` |
| opencode | the `OPENCODE_CONFIG` layer | the layer's `skills` | the layer's `instructions` |

`config-agents.sh` writes the two entries under the home on every start, and
removes the copies of the others that a home still holds. The image ships no
model credentials; users sign the agents in themselves.

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
