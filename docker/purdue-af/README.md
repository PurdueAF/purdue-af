# purdue-af — the Analysis Facility singleuser image

The image every AF session runs. Built on
`nvidia/cuda:12.4.1-devel-rockylinux8`, ~5 GB compressed. This directory is
self-contained: Dockerfile, jupyter configs, startup scripts, OSG rpms,
code-server assets, the pixi wrapper, and the CERN krb5/CA defaults under
`configs/`.

Contents beyond the CUDA base: the pixi `base-env` (`pixi/base/` — JupyterLab,
code-server, kernels), OSG grid clients (`voms-clients-cpp`, gfal2, xrootd),
Slurm client with Hammer configs, CVMFS/XRootD site config, and the coding
agents (Claude Code and Codex).

## Coding agents

The `claude` and `codex` CLIs are installed into `/opt/npm-global` with pinned
versions (`ARG CLAUDE_CODE_VERSION`, `ARG CODEX_VERSION`; Renovate bumps both).
Their code-server counterparts — `anthropic.claude-code` and `openai.chatgpt`,
the Open VSX IDs, not the MS Marketplace ones — are installed at startup by
`config-extensions.sh`.

`config-agents.sh` registers the AF MCP server with both on every start, which
covers the terminal and the editor at once because each extension reads the
config its CLI writes (`~/.claude.json`, `~/.codex/config.toml`). The server is
registered as `purdue-af-agentic-interface` — the name the bundled skill and
the repo's `.mcp.json` both use. It points at the in-cluster service address,
since `JUPYTERHUB_PUBLIC_HUB_URL` is empty inside a session:

    http://agentic-interface.${NAMESPACE}.svc.cluster.local:8888/services/agentic-interface/mcp

Neither config holds a token. Claude Code expands `${JUPYTERHUB_API_TOKEN}` in
the MCP header at read time and Codex takes `bearer_token_env_var`, so both
resolve the session's own token — which rotates on every spawn and would go
stale if it were baked into the persistent home directory. Users still have to
authenticate the agents themselves (`claude` / `codex login`); the image ships
no model credentials.

The same hook installs the bundled Claude Code skill into
`~/.claude/skills/`. There is one source of truth —
`.claude/skills/purdue-af-agentic-interface/SKILL.md`, which also serves people
working on this repo — and `prepare-skill.py` rewrites its preamble at build
time: the committed version opens with laptop setup steps (mint a token,
register the server by hand) that are already done inside a session. That
script fails the build if the preamble it expects is gone, so the setup
instructions can never silently reach users.

Codex has no skill mechanism, so it learns about the facility from its
instruction file instead. `managed-block.py` maintains a delimited section in
`~/.codex/AGENTS.md` and `~/.claude/CLAUDE.md`:

    <!-- BEGIN PURDUE AF — managed, edits inside are overwritten -->
    ...content from docker/purdue-af/agents/platform-context.md...
    <!-- END PURDUE AF -->

Everything inside the markers is replaced on every session start, so the AF
content tracks the image; everything outside is never touched, so users keep
their own instructions. The block is appended if the markers are absent, and
re-running is a no-op.

The block is [`platform-context.md`](agents/platform-context.md): the
facility's rules and guidance for an agent inside a session. Its numbers are
asserted against the hub and gateway values and the user docs by
`tests/manifests/test_platform_context.py`.

Notable constraints baked into the Dockerfile:

- Every `dnf` command runs with `--disablerepo=cuda` except the pinned cuDNN
  install. The base image ships NVIDIA's **rolling** rhel8 repo, which also
  carries CUDA 13.x whose `cccl` obsoletes `cuda-cccl-12-4`; an unconstrained
  `dnf upgrade` cross-grades CUDA and fails resolution.
- `--exclude='linux-firmware*'` on the upgrade — kernel firmware is useless
  in a container and costs >1 GB.
- `procps-ng psmisc krb5-workstation xz cpio libatomic` are installed
  explicitly: the CUDA base lacks them and sessions need them
  (`krb5-workstation` provides the `kinit`/`klist` that `eos-connect.sh` uses).
- `LIBRARY_PATH` points at CUDA stub libs (inherited from the base) so `nvcc`
  can link without a driver present.
- The image does **not** set `NVIDIA_VISIBLE_DEVICES`. The base bakes `all`,
  and the cluster runtime honours it, so 0-GPU sessions are guarded at spawn
  time instead: `gpu-availability.py`'s `modify_pod_hook` injects `void` into
  pods that request no GPU. GPU sessions get visibility from the device
  plugin's per-allocation injection.

## Build and publication

`ci.yml` owns this image end to end; there is no other build path. Its input
tree is this directory, `pixi/base/` and the Slurm inputs
(`.github/workflows/image-inputs.sh`). Two things are specific to this image:

- **Build** (`ci-images.yml`): buildx with the geddes `FROM` remapped to
  docker.io via a named context, a smoke stage that runs during the build
  (nvcc, ps, klist, xz, jupyterlab), and a single zstd upload of image and
  `mode=max` buildcache as deduplicated blobs.
- **e2e-pre-release** (`ci-e2e.yml`): pulls the `in-` image once, runs the
  CVMFS check (host CVMFS mounted in, `cmsset_default.sh` sourced), `kind
  load`s the same copy, and the hub-in-kind e2e spawns it through the hub's
  `pre-release` profile.

Which sessions pull which tag, the two-step release and rollback:
[RELEASING.md](../../RELEASING.md); registries: [REGISTRY.md](../REGISTRY.md).

## Release checklist

Before running **Release image**, verify on a test session: GPU visibility
(`nvidia-smi`, torch/TF), that a 0-GPU session sees no GPUs, `eos-connect.sh`
(kinit against CERN.CH), grid workflows (`voms-proxy-init`, gfal2, xrootd), and
Slurm (`sbatch`/`squeue`).
