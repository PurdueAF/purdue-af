#!/bin/bash
# Predicted Slurm wait per cluster, without submitting anything.
#
# `sbatch --test-only` validates the account/partition/QoS combination and
# reports when a job WOULD start. That catches what capacity metrics miss:
# Gautschi has been measured with 10,246 idle CPUs and a 3.4-day predicted
# start, because its standby QoS yields to owners.
#
# Runs inside the interlink-slurm-plugin image after /etc/startup.sh has
# installed this cluster's Slurm config and started munged. Writes one
# textfile that the sidecar serves; never submits work, never fails the pod.
set -uo pipefail

CLUSTER="${SLURM_CLUSTER:?SLURM_CLUSTER is required}"
FLAGS="${PROBE_SBATCH_FLAGS:?PROBE_SBATCH_FLAGS is required}"

# Labels are parsed from the flags rather than configured separately, so they
# cannot drift from what was actually submitted. The whole flag string also
# ships verbatim as `flags`, which is what the Grafana legend prints: users
# compare it against their own sbatch line. These three are the whole
# eligibility selector: Slurm has no "queue" distinct from the partition, and
# job shape does not move the prediction (measured: identical wait for
# 1 core/5 min and 64 cores/4 h on both Hammer and Gautschi).
flag_value() { printf '%s' "$FLAGS" | grep -oE -- "--$1=[^ ]+" | head -1 | cut -d= -f2; }
PARTITION="$(flag_value partition)"
QOS="$(flag_value qos)"
ACCOUNT="$(flag_value account)"
# Submitting as root tests root's associations, not a user's, and reports a
# false "Invalid account" — the plugin submits with --uid, so the probe must.
PROBE_UID="${PROBE_UID:-616617}"
INTERVAL="${PROBE_INTERVAL_S:-300}"
TIMEOUT="${PROBE_TIMEOUT_S:-30}"
OUT_DIR="${PROBE_OUT_DIR:-/var/lib/slurm-probes}"
OUT="${OUT_DIR}/${CLUSTER}.prom"
LABELS="cluster=\"${CLUSTER}\",account=\"${ACCOUNT}\",partition=\"${PARTITION}\",qos=\"${QOS}\",flags=\"${FLAGS}\""

probe_once() {
	local out rc now stamp start backlog tmp="${OUT}.tmp"

	out=$(timeout "$TIMEOUT" sbatch --test-only --uid="$PROBE_UID" \
		$FLAGS -N1 -n1 -t 5 --wrap="true" 2>&1)
	rc=$?
	now=$(date +%s)
	# "Job N to start at 2026-08-06T15:31:41 ... in partition hammer-nodes"
	stamp=$(printf '%s' "$out" |
		grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}' | head -1)

	if [ "$rc" -ne 0 ] || [ -z "$stamp" ]; then
		# No series at all: an unknown wait is not a short one, and a stale
		# file ages out of the sidecar on its own.
		echo "probe(${CLUSTER}): rc=${rc} ${out}" >&2
		return
	fi

	start=$(date -d "$stamp" +%s 2>/dev/null || echo "$now")
	backlog=$((start - now))
	[ "$backlog" -lt 0 ] && backlog=0

	# temp file + rename: the sidecar must never read a half-written file
	cat >"$tmp" <<-EOF
		# HELP af_slurm_backlog_seconds Predicted wait before a job would start.
		# TYPE af_slurm_backlog_seconds gauge
		af_slurm_backlog_seconds{${LABELS}} ${backlog}
	EOF
	mv -f "$tmp" "$OUT"
	echo "probe(${CLUSTER}): starts in ${backlog}s"
}

mkdir -p "$OUT_DIR"
while :; do
	probe_once
	sleep "$INTERVAL"
done
