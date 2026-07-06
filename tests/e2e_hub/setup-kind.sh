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

echo "==> kind cluster '$CLUSTER' (chart version $CHART_VERSION)"
kind get clusters 2>/dev/null | grep -qx "$CLUSTER" || kind create cluster --name "$CLUSTER" --wait 120s
kubectl config use-context "kind-$CLUSTER" >/dev/null

echo "==> pre-load singleuser image (keeps image pull out of the spawn window)"
docker pull -q "quay.io/jupyterhub/k8s-singleuser-sample:${CHART_VERSION}"
# Docker Desktop's containerd image store breaks `kind load` on multi-arch
# images ("content digest not found"); fall back to pulling on the node.
kind load docker-image --name "$CLUSTER" "quay.io/jupyterhub/k8s-singleuser-sample:${CHART_VERSION}" ||
	docker exec "${CLUSTER}-control-plane" crictl pull "quay.io/jupyterhub/k8s-singleuser-sample:${CHART_VERSION}"

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
sed "s/__CHART_VERSION__/${CHART_VERSION}/g" "$E2E_DIR/values-kind.yaml" >"$workdir/values-kind.yaml"

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
