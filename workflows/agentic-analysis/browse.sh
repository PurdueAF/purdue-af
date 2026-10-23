#!/usr/bin/env bash
# Opens the agentic-analysis volume read-only, in a pod of its own:
#   browse.sh               a shell at its root
#   browse.sh <command...>  that command there instead
#   browse.sh serve [port]  its files over HTTP at http://localhost:<port>
set -euo pipefail

image=geddes-registry.rcac.purdue.edu/ghcr-proxy-cache/purdueaf/agentic-analysis:latest
mount=/agentic-analysis
pod="agentic-analysis-browse-$(date +%s)"

overrides() { # <stdin> <tty> <command as a JSON array>
	cat <<JSON
{"spec": {
  "automountServiceAccountToken": false,
  "nodeSelector": {"cms-af-prod": "true"},
  "tolerations": [{"key": "hub.jupyter.org/dedicated", "operator": "Equal", "value": "cms-af", "effect": "NoSchedule"}],
  "securityContext": {"runAsNonRoot": true, "runAsUser": 1000, "runAsGroup": 1000},
  "containers": [{
    "name": "$pod", "image": "$image", "workingDir": "$mount",
    "stdin": $1, "stdinOnce": $1, "tty": $2, "command": $3,
    "resources": {"requests": {"cpu": "100m", "memory": "256Mi"}, "limits": {"memory": "1Gi"}},
    "volumeMounts": [{"name": "volume", "mountPath": "$mount", "readOnly": true}]
  }],
  "volumes": [{"name": "volume", "persistentVolumeClaim": {"claimName": "agentic-analysis", "readOnly": true}}]
}}
JSON
}

if [ "${1:-}" = serve ]; then
	port=${2:-8000}
	kubectl -n cms run "$pod" --restart=Never --image="$image" \
		--overrides="$(overrides false false "[\"python\", \"-m\", \"http.server\", \"$port\", \"--directory\", \"$mount\"]")"
	trap 'kubectl -n cms delete pod "$pod" --wait=false >/dev/null' EXIT
	kubectl -n cms wait --for=condition=Ready "pod/$pod" --timeout=4m
	echo "Serving at http://localhost:$port"
	kubectl -n cms port-forward "pod/$pod" "$port:$port"
else
	command='["bash"]'
	if [ $# -gt 0 ]; then
		command=$(python3 -c 'import json, sys; print(json.dumps(sys.argv[1:]))' "$@")
	fi
	tty=false
	if [ -t 0 ] && [ -t 1 ]; then
		tty=true
	fi
	kubectl -n cms run "$pod" --rm -i --tty="$tty" --restart=Never --image="$image" \
		--overrides="$(overrides true "$tty" "$command")"
fi
