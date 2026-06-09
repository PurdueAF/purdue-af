#!/usr/bin/env bash
#
# Validate everything Flux consumes, the same way Flux consumes it:
#
#   1. `kustomize build` each kustomize root under deploy/ (and standalone roots)
#   2. apply that environment's postBuild.substitute variables, parsed straight
#      from its flux-kustomization.yaml (no duplicated values — single source
#      of truth), via `flux envsubst --strict`
#   3. validate the rendered manifests with kubeconform against upstream
#      Kubernetes schemas + the CRDs-catalog (Flux CRDs, monitoring CRDs, ...)
#
# Bootstrap objects (flux-kustomization.yaml / git-repository.yaml) are
# validated directly, since they are applied out-of-band.
#
# Requirements: kustomize, flux, yq (v4), kubeconform
# Run locally:  ./scripts/validate-manifests.sh
#
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

KUBECONFORM=(
	kubeconform
	-summary
	-schema-location default
	-schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'
	# kagent CRDs have no published JSON schema (not in CRDs-catalog).
	-skip Agent,ModelConfig
)

failed=0

# --- Flux-managed environments -------------------------------------------
for env_dir in deploy/*/; do
	[[ -f "${env_dir}kustomization.yaml" ]] || continue
	name=$(basename "$env_dir")
	echo "──── ${name} ────"
	# Subshell: each environment gets exactly its own substitution variables.
	if ! (
		while IFS='=' read -r key value; do
			export "${key}=${value}"
		done < <(yq -r '.spec.postBuild.substitute // {} | to_entries[] | .key + "=" + .value' \
			"${env_dir}flux-kustomization.yaml")
		# LoadRestrictionsNone matches Flux's kustomize-controller behavior
		# (the deploy roots reference ../../apps/ files). Non-strict envsubst
		# also matches Flux: unset vars (Grafana $vars, PromQL $1, ...) pass
		# through untouched.
		kustomize build --load-restrictor LoadRestrictionsNone "$env_dir" |
			flux envsubst | "${KUBECONFORM[@]}"
	); then
		failed=1
	fi
done

# --- Standalone kustomize roots (applied out-of-band) --------------------
for root in docker/kaniko-build-jobs; do
	echo "──── ${root} ────"
	if ! kustomize build "$root" | "${KUBECONFORM[@]}"; then
		failed=1
	fi
done

# --- Flux bootstrap objects ----------------------------------------------
echo "──── flux bootstrap objects ────"
if ! "${KUBECONFORM[@]}" deploy/*/flux-kustomization.yaml deploy/*/git-repository.yaml; then
	failed=1
fi

if [[ $failed -ne 0 ]]; then
	echo "✗ manifest validation FAILED" >&2
	exit 1
fi
echo "✓ all manifests valid"
