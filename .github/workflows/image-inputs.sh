#!/usr/bin/env bash
#
# Input paths of every content-addressed image and memoized check (docker/REGISTRY.md).
# Each set includes the workflow that produces the artifact and this script.
#
# Usage:
#   image-inputs.sh <name>          prints "in-<12-hex>" for the current tree
#   image-inputs.sh --paths <name>  prints the input paths, one per line

set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

paths_for() {
	case "$1" in
	purdue-af)
		# Everything docker/purdue-af/Dockerfile COPYs or bind-mounts.
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
		# The skill versions with the server (RELEASING.md).
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
		# The Dockerfile COPYs the whole slurm/ tree.
		cat <<-EOF
			docker/interlink-slurm-plugin
			slurm
			.github/workflows/ci-images.yml
			.github/workflows/image-inputs.sh
		EOF
		;;
	interlink-kubernetes-plugin)
		cat <<-EOF
			docker/interlink-kubernetes-plugin
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
	agentic-analysis)
		# Runtime only: the workflow code reaches the task pods as a Flyte code bundle.
		cat <<-EOF
			docker/agentic-analysis
			.github/workflows/ci-images.yml
			.github/workflows/image-inputs.sh
		EOF
		;;
	ray-nrp)
		# The global environment, installed into the image.
		cat <<-EOF
			docker/ray-nrp
			pixi/global/pixi.toml
			pixi/global/pixi.lock
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
		# ci-pixi-global.yml appends the AF image to the memo key.
		cat <<-EOF
			pixi/global
			pixi/check-env.py
			tests/integration_challenge
			.github/workflows/ci-pixi-global.yml
			.github/workflows/image-inputs.sh
		EOF
		;;
	e2e-hub)
		# ci-e2e.yml appends the spawned image to the memo key.
		cat <<-EOF
			apps/jupyterhub/jupyterhub
			flux/core-production/kustomization.yaml
			tests/e2e_hub
			tests/pyproject.toml
			tests/uv.lock
			.github/actions/kind-hub-stack
			.github/actions/pick-af-image
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
