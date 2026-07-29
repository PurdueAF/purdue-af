#!/bin/bash
# Install the Slurm client config for $SLURM_CLUSTER, load the site munge key,
# and start munged. The sidecar binary is started by the image ENTRYPOINT after
# this script returns.
#
# Config resolution order:
#   1. /etc/secrets/slurm-configs/  (optional runtime override / out-of-band PVC)
#   2. /opt/purdue-af/slurm-configs/$SLURM_CLUSTER/  (baked from slurm/slurm-configs-*)

set -euo pipefail

sudo_cmd=""
if [ "$(id -u)" != "0" ]; then
	sudo_cmd="sudo"
	sudo -k
fi

configs_root="/opt/purdue-af/slurm-configs"
override="/etc/secrets/slurm-configs"

if [ -z "${SLURM_CLUSTER:-}" ]; then
	echo "SLURM_CLUSTER is unset. Set it to a Purdue cluster name (e.g. hammer)." >&2
	echo "Baked configs available:" >&2
	ls -1 "${configs_root}" >&2 || true
	exit 1
fi

src=""
if [ -f "${override}/slurm.conf" ]; then
	src="${override}"
	echo "Using Slurm client config override at ${override} (SLURM_CLUSTER=${SLURM_CLUSTER})"
elif [ -d "${configs_root}/${SLURM_CLUSTER}" ]; then
	src="${configs_root}/${SLURM_CLUSTER}"
	echo "Installing baked Slurm client config for cluster '${SLURM_CLUSTER}'"
else
	echo "No Slurm client config for SLURM_CLUSTER='${SLURM_CLUSTER}'." >&2
	echo "Add slurm/slurm-configs-${SLURM_CLUSTER}/ and rebuild, or mount a tree at ${override}." >&2
	echo "Baked configs available:" >&2
	ls -1 "${configs_root}" >&2 || true
	exit 1
fi

if [ ! -f "${src}/slurm.conf" ]; then
	echo "${src}/slurm.conf is missing — refusing to start" >&2
	exit 1
fi

${sudo_cmd} mkdir -p /etc/slurm
${sudo_cmd} rm -rf /etc/slurm/*
${sudo_cmd} cp -a "${src}/." /etc/slurm/
${sudo_cmd} chown -R slurm:slurm /etc/slurm

${sudo_cmd} mkdir -p /etc/munge
if [ ! -f /etc/secrets/munge/munge.key ]; then
	echo "missing /etc/secrets/munge/munge.key — mount munge-key-${SLURM_CLUSTER}" >&2
	exit 1
fi
${sudo_cmd} cp /etc/secrets/munge/munge.key /etc/munge/munge.key
${sudo_cmd} chown munge:munge /etc/munge/munge.key
${sudo_cmd} chmod 400 /etc/munge/munge.key

${sudo_cmd} bash <<'SCRIPT'
# UID used by AF test pods / interactive submitters (--uid=616617).
id -u 616617 >/dev/null 2>&1 || useradd -u 616617 -m dkondra
su -l munge -s /usr/sbin/munged &
SCRIPT

if [[ ${sudo_cmd} ]]; then
	sudo -k
fi
