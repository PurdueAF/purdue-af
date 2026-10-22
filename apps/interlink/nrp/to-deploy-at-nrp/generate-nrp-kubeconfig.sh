#!/usr/bin/env bash
CTX=nautilus # your kubelogin context for NRP
NS=interlink-cms
SA=interlink-plugin
OUT=$HOME/.kube/nrp-interlink-cms.yaml # keep outside the repo

SERVER=$(kubectl --context "$CTX" config view --minify --raw -o jsonpath='{.clusters[0].cluster.server}')
CA=$(kubectl --context "$CTX" config view --minify --raw -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')
if [ -z "$CA" ]; then # context points at a CA file instead of inline data
	CAFILE=$(kubectl --context "$CTX" config view --minify --raw -o jsonpath='{.clusters[0].cluster.certificate-authority}')
	CA=$(base64 <"$CAFILE" | tr -d '\n')
fi
TOKEN=$(kubectl --context "$CTX" -n "$NS" create token "$SA" --duration=720h)

umask 077
cat >"$OUT" <<EOF
apiVersion: v1
kind: Config
clusters:
- name: nrp
  cluster:
    server: $SERVER
    certificate-authority-data: $CA
users:
- name: $SA
  user:
    token: $TOKEN
contexts:
- name: nrp-$NS
  context:
    cluster: nrp
    user: $SA
    namespace: $NS
current-context: nrp-$NS
EOF

KUBECONFIG="$OUT" kubectl auth can-i --list -n "$NS"
