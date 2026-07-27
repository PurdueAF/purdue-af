#!/bin/bash
# Set up the coding agents for this session: register the Purdue AF MCP server
# with both CLIs (which also covers their code-server extensions, since each
# extension reads the config its CLI writes) and install the bundled skill.
#
# Neither config stores the token. Claude Code expands ${VAR} in MCP headers
# at read time and Codex reads `bearer_token_env_var` at connect time, so both
# pick up the session's own JUPYTERHUB_API_TOKEN — which rotates on every
# spawn and would otherwise go stale in the persistent home directory.
#
# Never fatal: a session must start even if an agent CLI is broken or the MCP
# service is mid-redeploy.
set -uo pipefail

# Must match the skill's references and the repo .mcp.json — the skill names
# this server explicitly, so a mismatch makes its instructions wrong.
MCP_NAME="purdue-af-agentic-interface"
# In-cluster address of the hub-registered service. The public URL is not
# usable from inside a session (JUPYTERHUB_PUBLIC_HUB_URL is empty there), and
# the service strips this prefix itself.
MCP_URL="http://agentic-interface.${NAMESPACE:-cms}.svc.cluster.local:8888/services/agentic-interface/mcp"
# Single-quoted so the placeholder reaches the config file verbatim — the
# agent expands it per run, which is the whole point.
AUTH_HEADER='Authorization: Bearer ${JUPYTERHUB_API_TOKEN}'
SKILL_SRC="/opt/purdue-af/skills"
AGENT_SECTION="/opt/purdue-af/agents/purdue-af-section.md"
# Absolute path on purpose: `su` resets PATH, and the system python3 on
# Rocky 8 is 3.6 — too old for the platform's scripts.
PYTHON="/opt/pixi/.pixi/envs/base-env/bin/python3"
[[ -x "${PYTHON}" ]] || PYTHON="python3"

if [[ -z "${NB_USER:-}" ]]; then
	echo "config-agents: NB_USER unset, skipping" >&2
	exit 0
fi
NEW_HOME="/home/${NB_USER}"

# The startup hooks run as root; drop to the session user so the configs land
# in their home with their ownership. Tests run this unprivileged.
_as_user() {
	if [[ ${EUID:-$(id -u)} -eq 0 ]]; then
		su "${NB_USER}" -c "$1"
	else
		bash -c "$1"
	fi
}

# `mcp add` owns the merge into files that also hold user state
# (~/.claude.json, ~/.codex/config.toml) — never rewrite those wholesale.
# Adding is not idempotent, so drop any previous entry first.
_register() {
	local tool="$1" remove="$2" add="$3"
	if ! command -v "${tool}" >/dev/null 2>&1; then
		echo "config-agents: ${tool} CLI not found, skipping" >&2
		return 0
	fi
	_as_user "${remove}" >/dev/null 2>&1 || true
	if _as_user "${add}" >/dev/null 2>&1; then
		echo "config-agents: registered '${MCP_NAME}' with ${tool}"
	else
		echo "config-agents: WARNING could not register MCP server with ${tool}" >&2
	fi
	return 0
}

_register claude \
	"claude mcp remove --scope user '${MCP_NAME}'" \
	"claude mcp add --scope user --transport http '${MCP_NAME}' '${MCP_URL}' --header '${AUTH_HEADER}'"

_register codex \
	"codex mcp remove '${MCP_NAME}'" \
	"codex mcp add '${MCP_NAME}' --url '${MCP_URL}' --bearer-token-env-var JUPYTERHUB_API_TOKEN"

# Claude Code skills, prepared at build time (prepare-skill.py). Refreshed on
# every start so an image upgrade ships an updated skill, the same way the
# Continue config is refreshed. Codex has no equivalent on-demand mechanism.
if [[ -d "${SKILL_SRC}" ]]; then
	if cp -r "${SKILL_SRC}/." "${NEW_HOME}/.claude/skills/" 2>/dev/null ||
		{ mkdir -p "${NEW_HOME}/.claude/skills" &&
			cp -r "${SKILL_SRC}/." "${NEW_HOME}/.claude/skills/"; }; then
		chown -R "${NB_USER}:users" "${NEW_HOME}/.claude" 2>/dev/null || true
		echo "config-agents: installed bundled skills into ${NEW_HOME}/.claude/skills"
	else
		echo "config-agents: WARNING could not install bundled skills" >&2
	fi
else
	echo "config-agents: no bundled skills at ${SKILL_SRC}, skipping" >&2
fi

# Agent instruction files belong to the user; only the AF block between the
# markers is ours. Codex has no skill mechanism, so this is the only place it
# learns about the facility; for Claude Code it is a short pointer alongside
# the skill.
if [[ -f "${AGENT_SECTION}" ]]; then
	for target in "${NEW_HOME}/.codex/AGENTS.md" "${NEW_HOME}/.claude/CLAUDE.md"; do
		if _as_user "'${PYTHON}' /usr/local/bin/managed-block.py '${AGENT_SECTION}' '${target}'"; then
			:
		else
			echo "config-agents: WARNING could not update ${target}" >&2
		fi
	done
	chown -R "${NB_USER}:users" "${NEW_HOME}/.claude" "${NEW_HOME}/.codex" 2>/dev/null || true
else
	echo "config-agents: no bundled agent section, skipping" >&2
fi

exit 0
