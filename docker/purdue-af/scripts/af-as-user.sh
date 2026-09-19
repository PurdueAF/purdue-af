#!/bin/bash
# Run a command as the notebook user; sourced by the before-notebook.d hooks.
# Hooks write below $HOME through this: part of a home may be symlinked onto
# /depot, NFS with root_squash, where root cannot write. Files directly in $HOME
# are on CephFS and stay as they are (.bashrc_af is root-owned on purpose).
af_as_user() {
	if [ "$(id -u)" -eq 0 ] && [ -n "${NB_USER:-}" ]; then
		runuser -u "$NB_USER" -- "$@"
	else
		"$@"
	fi
}
