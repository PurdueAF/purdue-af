#!/usr/bin/env bash
# Stand up the mock JupyterHub environment in a throwaway kind cluster:
# the chart version from helmrelease.yaml + real values.yaml + hub
# configmaps derived from the production kustomization, with CILogon and
# LDAP mocked and cluster-specifics overlaid away. Tests exactly what the
# repo deploys — CI passes no version parameter.
#
# CHART_VERSION=x.y.z is a LOCAL-ONLY override for interactive bisecting
# (e.g. Apple Silicon can't run hub images >= 4.3.5 — see README).
#
# Requirements: docker, kind, kubectl, helm, flux, yq
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

CLUSTER=af-e2e
HUB_DIR=apps/jupyterhub/jupyterhub
E2E_DIR=tests/e2e_hub
CHART_VERSION=${CHART_VERSION:-$(yq '.spec.chart.spec.version' "$HUB_DIR/helmrelease.yaml")}
workdir=$(mktemp -d)
trap 'rm -rf "$workdir"' EXIT

# The image production runs, derived from values.yaml (the single source of
# truth for the production pin) and remapped from the geddes
# ghcr-proxy-cache path to ghcr.io — GH runners cannot reach geddes, and
# the proxy mirrors ghcr, so the digest is identical. Override via env for
# local bisecting. Used by the "production" test profile in values-kind.yaml.
PROD_NAME=$(yq '.singleuser.image.name' "$HUB_DIR/values.yaml")
PROD_TAG=$(yq '.singleuser.image.tag' "$HUB_DIR/values.yaml")
PRODUCTION_IMAGE=${PRODUCTION_IMAGE:-${PROD_NAME/geddes-registry.rcac.purdue.edu\/ghcr-proxy-cache\//ghcr.io/}:${PROD_TAG}}
# name/tag split for singleuser.image (the chart wants them separate)
PRODUCTION_IMAGE_NAME=${PRODUCTION_IMAGE%:*}
PRODUCTION_IMAGE_TAG=${PRODUCTION_IMAGE##*:}

# Pre-pull a real AF image onto the kind node so it is present before the
# spawn window (crictl pull straight into containerd — faster than docker
# pull + kind load). Skips if already on the node; passes ghcr creds when
# provided (private-image / rate-limit safety).
preload_node() {
	local image=$1
	if docker exec "${CLUSTER}-control-plane" crictl inspecti "$image" >/dev/null 2>&1; then
		echo "==> AF image already on the node ($image)"
		return
	fi
	echo "==> pre-load AF image ($image)"
	local creds=()
	if [ -n "${GHCR_TOKEN:-}" ]; then
		creds=(--creds "${GHCR_USER:-token}:${GHCR_TOKEN}")
	fi
	docker exec "${CLUSTER}-control-plane" crictl pull "${creds[@]}" "$image"
}

echo "==> kind cluster '$CLUSTER' (chart version $CHART_VERSION)"
kind get clusters 2>/dev/null | grep -qx "$CLUSTER" || kind create cluster --name "$CLUSTER" --wait 120s
kubectl config use-context "kind-$CLUSTER" >/dev/null

# Real AF images (~5 GB compressed each): pre-loaded onto the node only for
# the job that actually spawns them, so local fast runs (and the other job)
# never pull 13 GB they will not use. Each job sets the marker for its own
# image; the e2e-pre-release job also `kind load`s its image beforehand
# (host daemon needs it for the CVMFS check), so preload_node no-ops there.
if [ -n "${PRERELEASE_IMAGE:-}" ]; then
	preload_node "$PRERELEASE_IMAGE"
fi
if [ -n "${PRELOAD_PRODUCTION:-}" ]; then
	preload_node "$PRODUCTION_IMAGE"
fi

echo "==> secrets and config (purdue: alice, bob, dkondra; cern: carol)"
kubectl create secret generic auth-secret \
	--from-literal=cilogon_client_id=mock-client \
	--from-literal=cilogon_client_secret=mock-secret \
	--dry-run=client -o yaml | kubectl apply -f -
kubectl create secret generic af-auth-purdue \
	--from-literal=userlist=$'alice\nbob\ndkondra\n' \
	--dry-run=client -o yaml | kubectl apply -f -
kubectl create secret generic af-auth-cern \
	--from-literal=userlist=$'carol\n' \
	--dry-run=client -o yaml | kubectl apply -f -
# Hub-mounted configmaps, derived from the production configMapGenerator so
# a snippet added (or renamed) there is automatically tested here — the
# harness must never drift from what Flux actually deploys. In kind:
# set-user-info.py talks to the mock LDAP (AF_LDAP_* env in
# values-kind.yaml); gpu-availability.py finds no Prometheus and fails open
# by design; the gpu-culler service idles (no pod ever holds a full GPU).
PROD_KUSTOMIZATION=deploy/core-production/kustomization.yaml
for cm in jupyterhub-extra-config jupyterhub-gpu-culler; do
	from_file_args=()
	while IFS= read -r entry; do
		key=${entry%%=*}
		path=${entry#*=}
		# generator paths are relative to the kustomization's directory
		from_file_args+=("--from-file=${key}=$(dirname "$PROD_KUSTOMIZATION")/${path}")
	done < <(yq ".configMapGenerator[] | select(.name == \"$cm\") | .files[]" "$PROD_KUSTOMIZATION")
	[ ${#from_file_args[@]} -gt 0 ] || {
		echo "no generator entry for $cm in $PROD_KUSTOMIZATION"
		exit 1
	}
	kubectl create configmap "$cm" "${from_file_args[@]}" \
		--dry-run=client -o yaml | kubectl apply -f -
done

echo "==> mock CILogon"
kubectl create configmap mock-cilogon \
	--from-file=mock-cilogon.py="$E2E_DIR/mock-cilogon.py" \
	--dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f "$E2E_DIR/mock-cilogon.yaml"

echo "==> mock LDAP (geddes-aux stand-in for set-user-info.py)"
kubectl create configmap mock-ldap \
	--from-file=10-users.ldif="$E2E_DIR/mock-ldap-users.ldif" \
	--from-file=20-acl.ldif="$E2E_DIR/mock-ldap-acl.ldif" \
	--dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f "$E2E_DIR/mock-ldap.yaml"

echo "==> render production values the way Flux does"
# Same mechanism as .github/workflows/validate-manifests.sh, with kind-appropriate vars.
export namespace=default
export enable_ingresses=false
export jupyterhub_enable_ingress=false
export jupyterhub_host=localhost
export jupyterhub_oauth_callback_url=http://localhost:8080/hub/oauth_callback
export jupyterhub_logout_redirect_url=http://localhost:8080/hub
export jupyterhub_db_storage_class=standard
export jupyterhub_db_pv_selector=""
export multinode_storage_class=standard
export singlenode_storage_class=standard
export af_shared_storage_size=1Gi
export x509_secret_name=af-x509-proxy
flux envsubst <"$HUB_DIR/values.yaml" >"$workdir/values.yaml"
sed -e "s|__PRERELEASE_IMAGE__|${PRERELEASE_IMAGE:-ghcr.io/purdueaf/purdue-af:pre-release}|g" \
	-e "s|__PRODUCTION_IMAGE__|${PRODUCTION_IMAGE}|g" \
	-e "s|__PRODUCTION_IMAGE_NAME__|${PRODUCTION_IMAGE_NAME}|g" \
	-e "s|__PRODUCTION_IMAGE_TAG__|${PRODUCTION_IMAGE_TAG}|g" \
	"$E2E_DIR/values-kind.yaml" >"$workdir/values-kind.yaml"

echo "==> helm install jupyterhub@${CHART_VERSION}"
helm repo add jupyterhub https://hub.jupyter.org/helm-chart/ >/dev/null 2>&1 || true
helm repo update jupyterhub >/dev/null
helm upgrade --install jupyterhub jupyterhub/jupyterhub \
	--version "$CHART_VERSION" \
	-f "$workdir/values.yaml" \
	-f "$workdir/values-kind.yaml" \
	--timeout 10m --wait

kubectl rollout status deployment/hub --timeout=300s
kubectl rollout status deployment/mock-cilogon --timeout=120s
kubectl rollout status deployment/mock-ldap --timeout=120s
echo "==> ready. Port-forward and run the tests:"
echo "    kubectl port-forward svc/proxy-public 8080:80 &"
echo "    kubectl port-forward svc/mock-cilogon 9090:9090 &"
echo "    E2E_HUB=1 uv run --project tests pytest tests/e2e_hub -v"
