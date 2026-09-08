#!/bin/bash
# Usage accounting for the agent CLIs, installed as /usr/local/bin/{claude,
# codex,opencode} to shadow /opt/npm-global/bin on PATH. Records each run and
# gives each agent its own OTel identity.
#
# Accounting, not enforcement: calling the real binary by its full path skips
# it. Nothing here may break the agent; the exit status is always its own.

set -u

_paf_agent="${0##*/}"
_paf_prefix="${NPM_GLOBAL:-/opt/npm-global}"
_paf_real="${_paf_prefix}/bin/${_paf_agent}"

if [[ ! -x "${_paf_real}" ]]; then
	echo "purdue-af: ${_paf_agent} is not installed in this image" >&2
	exit 127
fi

# Baked in at build time, so this never spawns `--version`.
_paf_version="unknown"
if [[ -r "${_paf_prefix}/versions.env" ]]; then
	# shellcheck disable=SC1091  # generated at image build time
	source "${_paf_prefix}/versions.env" 2>/dev/null || true
	_paf_var="PAF_VERSION_${_paf_agent//-/_}"
	_paf_version="${!_paf_var:-unknown}"
fi

_paf_user="${NB_USER:-${USER:-unknown}}"
_paf_user="${_paf_user//[\"\\]/}" # cannot break out of the JSON string

# PID 1's stdout, not this terminal; Alloy ships it to Loki from there.
# PAF_USAGE_SINK is for the tests.
_paf_sink="${PAF_USAGE_SINK:-/proc/1/fd/1}"
_paf_emit() {
	printf '{"event":"agent_run","phase":"%s","agent":"%s","version":"%s","user":"%s"%s}\n' \
		"$1" "${_paf_agent}" "${_paf_version}" "${_paf_user}" "${2:-}" \
		>>"${_paf_sink}" 2>/dev/null || true
}

# OTEL_SERVICE_NAME is absent from the pod env so it can mean "this agent".
# Appended to, not replaced, so a user's own attributes survive.
export OTEL_SERVICE_NAME="${_paf_agent}"
_paf_attrs="user=${_paf_user},af.agent=${_paf_agent},af.facility=purdue-af"
if [[ -n "${OTEL_RESOURCE_ATTRIBUTES:-}" ]]; then
	export OTEL_RESOURCE_ATTRIBUTES="${OTEL_RESOURCE_ATTRIBUTES},${_paf_attrs}"
else
	export OTEL_RESOURCE_ATTRIBUTES="${_paf_attrs}"
fi

_paf_emit start ""

# Not `trap ''`: an ignored signal is inherited, leaving the agent deaf to ^C.
trap ':' INT TERM

# Not $EPOCHREALTIME, which needs bash 5.0; this image is Rocky 8.
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
