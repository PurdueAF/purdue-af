#!/bin/bash

# Clear the runtime state the previous session left in the user's home.
#
# Remote-SSH servers, code-server, Claude Code, Codex and Copilot all drop
# lock files, unix sockets, pid-named logs and half-written temp files into
# $HOME. Every one of them describes a process that died with the pod, so on
# the next start they are not just wasted space — they actively mislead: a
# stale ~/.claude/ide/<pid>.lock advertises an IDE port nothing is listening
# on, and a leftover ~/.codex/ipc/ipc.sock is a socket with no peer.
#
# The list below is deliberately narrow. Only paths whose entire contents are
# recreated by the tool that owns them are named here — no caches, no history,
# no session transcripts, and nothing that holds user work. In particular
# ~/.claude/jobs is NOT touched: it looks like scratch space, but the `tmp`
# directory inside each job holds scripts the user wrote and may still want.
#
# start.sh SOURCES this file under `set -e`, so nothing here may exit non-zero:
# a failed rm must cost the user some disk space, never their session.

_CSS_HOME="/home/${NB_USER}"

# Directories emptied but kept: everything inside is per-process runtime state.
_css_dirs=(
	".claude/ide"   # <pid>.lock files pointing at the IDE's port
	".codex/ipc"    # unix sockets
	".codex/tmp"    # scratch for the running CLI
	".copilot/logs" # process-<epoch>-<pid>.log
	".vscode-server/data/logs"
	".cursor-server/data/logs"
	".git-credential-cache" # the cache daemon's socket
)

# Globs removed outright: locks, logs and interrupted atomic writes.
_css_globs=(
	".claude/daemon.lock"
	".claude/daemon.log"
	".claude/daemon.status.json"
	".claude.json.tmp.*"
	".claude/.credentials.json.tmp.*"
	".codex/.tmp/*.lock"
	".vscode-server/.cli.*.log"
	".vscode-server/cli/*.lock"
	".vscode-server/cli/*.log"
	".cursor-server/cli/*.lock"
	".cursor-server/cli/*.log"
	".bash_history-*.tmp"
	".python_history-*.tmp"
)

_css_removed=0

# Never follow a symlink out of the home: remove the link, not what it points
# at. Failures are counted as skipped — /depot is NFS with root_squash and this
# hook runs as root, so a home whose dotfiles live on depot simply cannot be
# cleaned from here.
_css_rm() {
	local path="$1"
	[ -e "$path" ] || [ -L "$path" ] || return 0
	if rm -rf -- "$path" 2>/dev/null; then
		_css_removed=$((_css_removed + 1))
	fi
	return 0
}

if [ -n "${NB_USER:-}" ] && [ -d "$_CSS_HOME" ]; then
	for _css_d in "${_css_dirs[@]}"; do
		_css_target="$_CSS_HOME/$_css_d"
		# A symlinked directory is the user's own arrangement; leave it alone
		# rather than deleting through it.
		[ -d "$_css_target" ] && [ ! -L "$_css_target" ] || continue
		for _css_entry in "$_css_target"/* "$_css_target"/.[!.]*; do
			[ -e "$_css_entry" ] || [ -L "$_css_entry" ] || continue
			_css_rm "$_css_entry"
		done
	done

	for _css_g in "${_css_globs[@]}"; do
		for _css_entry in "$_CSS_HOME"/$_css_g; do
			_css_rm "$_css_entry"
		done
	done

	if [ "$_css_removed" -gt 0 ]; then
		echo "clean-session-state: removed $_css_removed stale runtime entries from $_CSS_HOME"
	fi
else
	echo "clean-session-state: no home for '${NB_USER:-}', nothing to clean" >&2
fi

unset _CSS_HOME _css_dirs _css_globs _css_removed _css_d _css_g _css_target _css_entry
