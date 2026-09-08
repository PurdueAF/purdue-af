#!/bin/bash
# Usage accounting for the coding-agent CLIs, installed as /usr/local/bin/claude,
# /usr/local/bin/codex and /usr/local/bin/opencode — which shadow the real
# binaries in /opt/npm-global/bin because ENV PATH puts /usr/local/bin first.
#
# It does two things the agents cannot do for themselves:
#
#   1. Records that an agent ran, for how long, and how it exited. opencode
#      exports no OpenTelemetry at all, so for that agent this is the only
#      signal there is; for claude and codex it is a cross-check on a stream
#      the user can switch off.
#   2. Gives each agent its own OTel identity — OTEL_SERVICE_NAME, and the AF
#      username in OTEL_RESOURCE_ATTRIBUTES — so its telemetry can be joined
#      to every other per-user metric in the facility. Setting these here
#      rather than pod-wide keeps them off jupyter-server's own tracer.
#
# This is accounting, not enforcement: a user who runs the real binary by its
# full path skips the wrapper, and that is fine. The record is written to the
# container's stdout (PID 1), which Alloy already ships to Loki — never to the
# user's terminal, which the agent owns.
#
# Nothing here may break the agent. Every added step is guarded, and the exit
# status is always the agent's own.

set -u

_paf_agent="${0##*/}"
# NPM_GLOBAL is set by the image (ENV NPM_GLOBAL=/opt/npm-global); the default
# keeps the wrapper working if it is ever run with a stripped environment.
_paf_prefix="${NPM_GLOBAL:-/opt/npm-global}"
_paf_real="${_paf_prefix}/bin/${_paf_agent}"

# Without the real binary there is nothing to wrap and nothing to report.
if [[ ! -x "${_paf_real}" ]]; then
	echo "purdue-af: ${_paf_agent} is not installed in this image" >&2
	exit 127
fi

# Versions are baked in at build time (Dockerfile ARGs) so the wrapper never
# pays for a `--version` subprocess on a path the user is waiting on.
_paf_version="unknown"
if [[ -r "${_paf_prefix}/versions.env" ]]; then
	# shellcheck disable=SC1091  # generated at image build time
	source "${_paf_prefix}/versions.env" 2>/dev/null || true
	_paf_var="PAF_VERSION_${_paf_agent//-/_}"
	_paf_version="${!_paf_var:-unknown}"
fi

_paf_user="${NB_USER:-${USER:-unknown}}"
# JSON string context: strip the only characters that could break out of it.
_paf_user="${_paf_user//[\"\\]/}"

# One line per event on the container's stdout, which is a different stream
# from this terminal — Alloy ships it to Loki from there. If that is not
# writable (the wrapper run outside a session), the record is simply dropped:
# accounting must never be the reason an agent fails to start.
# PAF_USAGE_SINK exists so the test suite can read what this writes.
_paf_sink="${PAF_USAGE_SINK:-/proc/1/fd/1}"
_paf_emit() {
	printf '{"event":"agent_run","phase":"%s","agent":"%s","version":"%s","user":"%s"%s}\n' \
		"$1" "${_paf_agent}" "${_paf_version}" "${_paf_user}" "${2:-}" \
		>>"${_paf_sink}" 2>/dev/null || true
}

# Identify this agent's telemetry. OTEL_SERVICE_NAME is deliberately absent
# from the pod environment so that it means "this agent" here and defaults to
# "jupyter-server" for the notebook server's own tracer; OTEL_RESOURCE_ATTRIBUTES
# is appended to rather than replaced, so a user's own attributes survive.
export OTEL_SERVICE_NAME="${_paf_agent}"
_paf_attrs="user=${_paf_user},af.agent=${_paf_agent},af.facility=purdue-af"
if [[ -n "${OTEL_RESOURCE_ATTRIBUTES:-}" ]]; then
	export OTEL_RESOURCE_ATTRIBUTES="${OTEL_RESOURCE_ATTRIBUTES},${_paf_attrs}"
else
	export OTEL_RESOURCE_ATTRIBUTES="${_paf_attrs}"
fi

_paf_emit start ""

# A trap with a real action (not '') is reset to the default disposition in
# child processes, so the agent still receives Ctrl-C normally — while this
# shell survives to write the stop record. `trap '' INT` would be inherited as
# "ignored" and would make the agent itself deaf to interrupts.
trap ':' INT TERM

# Milliseconds since the epoch. Not $EPOCHREALTIME: that is bash 5.0+, and
# this image is Rocky 8 (bash 4.4). GNU date has %N; anything else falls back
# to the SECONDS builtin, which costs precision but never fails.
_paf_now_ms() {
	local now
	now="$(date +%s%N 2>/dev/null)"
	if [[ "${now}" =~ ^[0-9]{10,}$ ]]; then
		echo $((now / 1000000))
	else
		echo ""
	fi
}

SECONDS=0
_paf_start_ms="$(_paf_now_ms)"
"${_paf_real}" "$@"
_paf_status=$?
_paf_end_ms="$(_paf_now_ms)"
if [[ -n "${_paf_start_ms}" && -n "${_paf_end_ms}" ]]; then
	_paf_duration=$((_paf_end_ms - _paf_start_ms))
else
	_paf_duration=$((SECONDS * 1000))
fi

_paf_emit stop ",\"duration_ms\":${_paf_duration},\"exit_code\":${_paf_status}"

exit "${_paf_status}"
