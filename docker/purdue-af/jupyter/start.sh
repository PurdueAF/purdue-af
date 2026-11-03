#!/bin/bash
# Copyright (c) Jupyter Development Team.
# Distributed under the terms of the Modified BSD License.

set -e

echo "Entered start.sh with args:" "$@"

# Sourced so their exports reach the server. A failed step is logged and the hook
# carries on: it costs the session that step, never the session. Only `exit` ends this script.
run-hooks() {
	if [[ ! -d "${1}" ]]; then
		return
	fi
	echo "${0}: running hooks in ${1} as uid / gid: $(id -u) / $(id -g)"
	for f in "${1}/"*.sh; do
		echo "${0}: running script ${f}"
		set +e -E
		trap 'echo "WARNING: ${BASH_SOURCE[0]}:${LINENO}: exit status $?: ${BASH_COMMAND}" >&2' ERR
		# shellcheck disable=SC1090
		source "${f}"
		trap - ERR
		set -e +E
	done
	echo "${0}: done running hooks in ${1}"
}

# Default to starting bash if no command was specified
if [ $# -eq 0 ]; then
	cmd=("bash")
else
	cmd=("$@")
fi

# Without root, a bare `docker run` of the image: nothing to refit, and no hooks.
if [ "$(id -u)" != 0 ]; then
	exec "${cmd[@]}"
fi

# Refit the jovyan user to the desired user (NB_USER)
if id jovyan &>/dev/null; then
	if ! usermod --home "/home/${NB_USER}" --login "${NB_USER}" jovyan 2>&1 | grep "no changes" >/dev/null; then
		echo "Updated the jovyan user:"
		echo "- username: jovyan       -> ${NB_USER}"
		echo "- home dir: /home/jovyan -> /home/${NB_USER}"
	fi
elif ! id -u "${NB_USER}" &>/dev/null; then
	echo "ERROR: Neither the jovyan user or '${NB_USER}' exists. This could be the result of stopping and starting, the container with a different NB_USER environment variable."
	exit 1
fi
# Ensure the desired user (NB_USER) gets its desired user id (NB_UID) and is
# a member of the desired group (NB_GROUP, NB_GID)
if [ "${NB_UID}" != "$(id -u "${NB_USER}")" ] || [ "${NB_GID}" != "$(id -g "${NB_USER}")" ]; then
	echo "Update ${NB_USER}'s UID:GID to ${NB_UID}:${NB_GID}"
	# Ensure the desired group's existence
	if [ "${NB_GID}" != "$(id -g "${NB_USER}")" ]; then
		groupadd --force --gid "${NB_GID}" --non-unique "${NB_GROUP:-${NB_USER}}"
	fi
	# Recreate the desired user as we want it
	userdel "${NB_USER}"
	useradd --home "/home/${NB_USER}" --uid "${NB_UID}" --gid "${NB_GID}" --groups 100 --no-log-init "${NB_USER}"
fi

# KubeSpawner mounts the home at its escaped name, which differs from NB_USER
# for a name that is not a valid DNS label.
mkdir -p "/home/${NB_USER}"

if [[ "${CHOWN_HOME}" == "1" || "${CHOWN_HOME}" == "yes" ]]; then
	echo "Ensuring /home/${NB_USER} is owned by ${NB_UID}:${NB_GID}"
	chown "${NB_UID}:${NB_GID}" "/home/${NB_USER}"
fi

# The image sets HOME for jovyan; this follows NB_USER.
export XDG_CACHE_HOME="/home/${NB_USER}/.cache"

run-hooks /usr/local/bin/before-notebook.d

echo "Running as ${NB_USER}:" "${cmd[@]}"
# sudo's environment rules drop PATH and PYTHONPATH, so they are passed explicitly.
exec sudo --preserve-env --set-home --user "${NB_USER}" \
	PATH="${PATH}" \
	PYTHONPATH="${PYTHONPATH:-}" \
	"${cmd[@]}"
