#!/usr/bin/env bash
# Stand up the mock JupyterHub environment in a throwaway kind cluster:
# real chart version + real values.yaml + byte-identical auth scripts,
# with CILogon mocked and cluster-specifics overlaid away.
#
#   CHART_VERSION=4.3.5 tests/e2e_hub/setup-kind.sh   # test a chart upgrade
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

echo "==> secrets and config (test users: alice, bob; cern: carol)"
kubectl create secret generic auth-secret \
	--from-literal=cilogon_client_id=mock-client \
	--from-literal=cilogon_client_secret=mock-secret \
	--dry-run=client -o yaml | kubectl apply -f -
kubectl create secret generic af-auth-purdue \
	--from-literal=userlist=$'alice\nbob' \
	--dry-run=client -o yaml | kubectl apply -f -
kubectl create secret generic af-auth-cern \
	--from-literal=userlist=$'carol' \
	--dry-run=client -o yaml | kubectl apply -f -
# Phase 1 ships only the spawner; set-user-info needs the LDAP mock (phase 2).
kubectl create configmap jupyterhub-extra-config \
	--from-file=00-custom-spawner.py="$HUB_DIR/extraFiles/custom-spawner.py" \
	--dry-run=client -o yaml | kubectl apply -f -

echo "==> mock CILogon"
kubectl create configmap mock-cilogon \
	--from-file=mock-cilogon.py="$E2E_DIR/mock-cilogon.py" \
	--dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f "$E2E_DIR/mock-cilogon.yaml"

echo "==> render production values the way Flux does"
# Same mechanism as scripts/validate-manifests.sh, with kind-appropriate vars.
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
echo "==> ready. Port-forward and run the tests:"
echo "    kubectl port-forward svc/proxy-public 8080:80 &"
echo "    kubectl port-forward svc/mock-cilogon 9090:9090 &"
echo "    E2E_HUB=1 uv run --project tests pytest tests/e2e_hub -v"
