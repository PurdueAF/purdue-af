#!/usr/bin/env bash
set -euo pipefail

if [ -n "${MOCK_DOCKER_LOG:-}" ]; then
  printf '%s\n' "$*" >> "$MOCK_DOCKER_LOG"
fi

cmd="${1:-}"
shift || true

case "$cmd" in
  image)
    subcmd="${1:-}"
    shift || true
    if [ "$subcmd" != "inspect" ]; then
      echo "mock docker unsupported image subcommand: $subcmd" >&2
      exit 64
    fi

    if [ -n "${MOCK_DOCKER_INSPECT_STDOUT:-}" ]; then
      printf '%s\n' "$MOCK_DOCKER_INSPECT_STDOUT"
    fi
    if [ -n "${MOCK_DOCKER_INSPECT_STDERR:-}" ]; then
      printf '%s\n' "$MOCK_DOCKER_INSPECT_STDERR" >&2
    fi

    exit "${MOCK_DOCKER_INSPECT_EXIT:-0}"
    ;;

  run)
    if [ -n "${MOCK_DOCKER_RUN_STDOUT:-}" ]; then
      printf '%s\n' "$MOCK_DOCKER_RUN_STDOUT"
    fi
    if [ -n "${MOCK_DOCKER_RUN_STDERR:-}" ]; then
      printf '%s\n' "$MOCK_DOCKER_RUN_STDERR" >&2
    fi

    exit "${MOCK_DOCKER_RUN_EXIT:-0}"
    ;;

  *)
    echo "mock docker unsupported command: $cmd" >&2
    exit 64
    ;;
esac
