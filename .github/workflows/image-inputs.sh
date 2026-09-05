#!/usr/bin/env bash
#
# Single source of truth for CONTENT-ADDRESSED CI artifacts.
#
# Every publishable artifact (image) and every memoized check (pixi, e2e)
# declares its input paths here. The hash of those paths' git state names
# the artifact: images are tagged `in-<hash>` on ghcr, checks record green
# runs under cache keys containing the same hash. A build or test is
# skipped ONLY when the artifact/marker for the exact current input state
# already exists — verified reuse, never "the path filter didn't match".
#
# Usage:
#   image-inputs.sh <name>          → prints "in-<12-hex>" for the current tree
#   image-inputs.sh --paths <name>  → prints the input path list (one per line)
#
# Names: purdue-af, agentic-interface, af-pod-monitor, af-node-monitor,
#        supersonic-model-manager, interlink-slurm-plugin, pixi-base,
#        pixi-global, e2e-hub
#
# The hash covers file content, names and modes of every TRACKED file under
# the listed pathspecs (git ls-files -s), so it is independent of commit
# history: a squash-merge with an identical tree reuses the PR's artifacts.
# Each set includes the workflow that produces the artifact and this script,
# so changing the build/test logic also invalidates the artifact.

set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

paths_for() {
	case "$1" in
	purdue-af)
		# Everything COPY'd by docker/purdue-af/Dockerfile — the dir is
		# self-contained (Dockerfile + scripts/jupyter/osg/xml/code-server/
		# configs/pixi-wrapper), plus the pixi base env and Slurm inputs.
		cat <<-EOF
			docker/purdue-af
			.claude/skills/purdue-af-agentic-interface
			pixi/base
			pixi/check-env.py
			slurm/slurm-configs-hammer
			slurm/*.rpm
			.github/workflows/ci-images.yml
			.github/workflows/image-inputs.sh
		EOF
		;;
	agentic-interface)
		# The skill is the user-facing contract of the MCP server, versioned
		# WITH it: a skill change alters this hash, so the publish stage's
		# auto-release mints a new version (minor for skill changes — see
		# ci.yml) even when the server code is untouched.
		cat <<-EOF
			docker/agentic-interface
			apps/jupyterhub/jupyterhub/extraFiles/gpu_queries.py
			.claude/skills/purdue-af-agentic-interface
			.github/workflows/ci-images.yml
			.github/workflows/image-inputs.sh
		EOF
		;;
	af-pod-monitor)
		cat <<-EOF
			docker/af-pod-monitor
			.github/workflows/ci-images.yml
			.github/workflows/image-inputs.sh
		EOF
		;;
	supersonic-model-manager)
		cat <<-EOF
			docker/supersonic-model-manager
			.github/workflows/ci-images.yml
			.github/workflows/image-inputs.sh
		EOF
		;;
	interlink-slurm-plugin)
		# Dockerfile COPY's the whole slurm/ tree (client RPM + every
		# slurm-configs-<cluster>/); a new cluster config must invalidate
		# the image hash the same way a Dockerfile change does.
		cat <<-EOF
			docker/interlink-slurm-plugin
			slurm
			.github/workflows/ci-images.yml
			.github/workflows/image-inputs.sh
		EOF
		;;
	af-node-monitor)
		cat <<-EOF
			docker/af-node-monitor
			.github/workflows/ci-images.yml
			.github/workflows/image-inputs.sh
		EOF
		;;
	pixi-base)
		cat <<-EOF
			pixi/base
			pixi/check-env.py
			.github/workflows/ci-pixi-base.yml
			.github/workflows/image-inputs.sh
		EOF
		;;
	pixi-global)
		# The AF image the check runs inside is appended to the memo key
		# by ci-pixi-global.yml (it is not a git path). tests/integration_
		# challenge covers the analysis smoke test that shares this job:
		# its driver AND upstream.pin, so a Renovate bump of the pinned
		# challenge revision invalidates the marker and forces a real run.
		cat <<-EOF
			pixi/global
			pixi/check-env.py
			tests/integration_challenge
			.github/workflows/ci-pixi-global.yml
			.github/workflows/image-inputs.sh
		EOF
		;;
	e2e-hub)
		# Everything the kind e2e deploys or executes. The AF image tag is
		# appended to the memo key by ci-e2e.yml for the real-image job.
		cat <<-EOF
			apps/jupyterhub/jupyterhub
			deploy/core-production/kustomization.yaml
			tests/e2e_hub
			tests/pyproject.toml
			tests/uv.lock
			.github/workflows/ci-e2e.yml
			.github/workflows/image-inputs.sh
		EOF
		;;
	*)
		echo "image-inputs.sh: unknown artifact '$1'" >&2
		exit 1
		;;
	esac
}

if [[ "${1:-}" == "--paths" ]]; then
	paths_for "${2:?usage: image-inputs.sh --paths <name>}"
	exit 0
fi

name="${1:?usage: image-inputs.sh [--paths] <name>}"
mapfile -t pathspecs < <(paths_for "$name")

listing=$(git ls-files -s -- "${pathspecs[@]}")
if [[ -z "$listing" ]]; then
	echo "image-inputs.sh: no tracked files matched inputs of '$name'" >&2
	exit 1
fi
hash=$(printf '%s\n' "$listing" | git hash-object --stdin)
echo "in-${hash:0:12}"
