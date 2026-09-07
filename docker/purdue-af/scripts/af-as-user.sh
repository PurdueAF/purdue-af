#!/bin/bash
# Run a command as the notebook user. Sourced by the before-notebook.d hooks,
# never executed on its own.
#
# The hooks run as root, but most of what they write lives in the user's own
# $HOME. Doing that as root is wrong twice over: it leaves root-owned files in
# the user's home, and it fails outright when the user has symlinked part of
# their home onto /depot to stay under quota. /depot is NFS with root_squash,
# so root is the one identity that cannot write there — the user can.
#
# That failure is expensive to diagnose: start.sh sources the hooks under
# `set -e`, so one EACCES exits before JupyterLab binds its port, the container
# crash-loops, and the hub reports only "server didn't respond in 600 seconds".
#
# runuser rather than su: the image adds pam_deny.so to /etc/pam.d/su, and
# runuser has its own PAM config. It preserves PATH and sets HOME to the user's.
#
# Only for paths *below* $HOME. Files directly in $HOME always sit on the
# CephFS home volume, where root can write, and some of them (.bashrc_af) are
# deliberately root-owned.
af_as_user() {
	if [ "$(id -u)" -eq 0 ] && [ -n "${NB_USER:-}" ]; then
		runuser -u "$NB_USER" -- "$@"
	else
		"$@"
	fi
}
