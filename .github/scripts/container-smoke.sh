#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 2 ]; then
  echo "Usage: $0 <image> <profile>" >&2
  exit 2
fi

image="$1"
profile="$2"

docker image inspect "$image" >/dev/null

case "$profile" in
  af-pod-monitor)
    docker run --rm --entrypoint python "$image" -c "import prometheus_client"
    ;;
  interlink-slurm-plugin)
    docker run --rm --entrypoint /bin/sh "$image" -lc 'test -x /sidecar/slurm-sidecar'
    ;;
  purdue-af)
    docker run --rm --entrypoint /bin/bash "$image" -lc 'python --version && jupyter --version >/dev/null'
    ;;
  *)
    echo "Unknown profile: $profile" >&2
    exit 2
    ;;
esac

echo "Smoke checks passed for profile: $profile"
