#!/bin/bash
# Set up the coding agents for this session: register the Purdue AF MCP server
# with each CLI (which also covers their code-server extensions, since each
# extension reads the config its CLI writes), install the bundled skill, and
# write the platform context into the file every harness reads automatically.
#
# No config stores the token. Claude Code expands ${VAR} in MCP headers at read
# time, Codex reads `bearer_token_env_var` at connect time, and opencode expands
# `{env:...}`, so all three pick up the session's own JUPYTERHUB_API_TOKEN —
# which rotates on every spawn and would otherwise go stale in the persistent
# home directory.
#
# Never fatal: a session must start even if an agent CLI is broken or the MCP
# service is mid-redeploy.
#
# start.sh SOURCES every *.sh in before-notebook.d, so this file runs in the
# startup shell itself: a top-level `exit` would terminate the container
# before JupyterLab ever launches, and `set -u`/`set -e` would leak into the
# rest of start.sh. Everything therefore lives in a function that returns.

_config_agents() {
	# Must match the skill's references and the repo .mcp.json — the skill names
	# this server explicitly, so a mismatch makes its instructions wrong.
	local MCP_NAME MCP_URL AUTH_HEADER SKILL_SRC AGENT_SECTION PYTHON NEW_HOME
	local OPENCODE_CFG target
	MCP_NAME="purdue-af-agentic-interface"
	# In-cluster address of the hub-registered service. The public URL is not
	# usable from inside a session (JUPYTERHUB_PUBLIC_HUB_URL is empty there), and
	# the service strips this prefix itself.
	MCP_URL="http://agentic-interface.${NAMESPACE:-cms}.svc.cluster.local:8888/services/agentic-interface/mcp"
	# Single-quoted so the placeholder reaches the config file verbatim — the
	# agent expands it per run, which is the whole point.
	AUTH_HEADER='Authorization: Bearer ${JUPYTERHUB_API_TOKEN}'
	SKILL_SRC="/opt/purdue-af/skills"
	AGENT_SECTION="/opt/purdue-af/agents/platform-context.md"
	# Absolute path on purpose: `su` resets PATH, and the system python3 on
	# Rocky 8 is 3.6 — too old for the platform's scripts.
	PYTHON="/opt/pixi/.pixi/envs/base-env/bin/python3"
	[[ -x "${PYTHON}" ]] || PYTHON="python3"

	if [[ -z "${NB_USER:-}" ]]; then
		echo "config-agents: NB_USER unset, skipping" >&2
		return 0
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

	# opencode has no `mcp add`, and it does not need one: OPENCODE_CONFIG names a
	# config layer that opencode merges BETWEEN the user's global config and their
	# project config, so the facility gets a file of its own and never edits
	# theirs. Written even when the CLI is absent — a user who installs opencode
	# into their own home afterwards finds it already wired up.
	#
	# `instructions` is what carries the platform context into a project that has
	# its own AGENTS.md: opencode's rules lookup is first-match-wins, so a project
	# AGENTS.md suppresses the global one, and the guardrails would vanish exactly
	# where an analysis repo needs them.
	OPENCODE_CFG="${NEW_HOME}/.config/opencode/purdue-af.json"
	# The heredoc is unquoted so ${MCP_URL} expands; \$schema must not.
	if mkdir -p "${NEW_HOME}/.config/opencode" && cat >"${OPENCODE_CFG}" <<-JSON
		{
		  "\$schema": "https://opencode.ai/config.json",
		  "instructions": ["${AGENT_SECTION}"],
		  "mcp": {
		    "${MCP_NAME}": {
		      "type": "remote",
		      "url": "${MCP_URL}",
		      "enabled": true,
		      "headers": {
		        "Authorization": "Bearer {env:JUPYTERHUB_API_TOKEN}"
		      }
		    }
		  }
		}
	JSON
	then
		# Written as root, so hand both directories back: ~/.config may not have
		# existed, and a root-owned one would block every later write the user
		# makes anywhere under it. Non-recursive on ~/.config itself — it is
		# shared with unrelated tools whose ownership is not ours to rewrite.
		chown "${NB_USER}:users" "${NEW_HOME}/.config" 2>/dev/null || true
		chown -R "${NB_USER}:users" "${NEW_HOME}/.config/opencode" 2>/dev/null || true
		# Exported, not baked into the image: NAMESPACE is templated per
		# deployment and the path depends on the session user. start.sh sources
		# this hook and then execs `sudo --preserve-env`, so the export reaches
		# the notebook server and every terminal under it.
		export OPENCODE_CONFIG="${OPENCODE_CFG}"
		echo "config-agents: registered '${MCP_NAME}' with opencode"
	else
		echo "config-agents: WARNING could not write ${OPENCODE_CFG}" >&2
	fi

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

	# One file per harness: the path it reads automatically at user scope, with no
	# skill and no prompting. These files belong to the user — only the AF block
	# between the markers is ours. Codex and opencode have no skill mechanism, so
	# this is the only place they learn about the facility; for Claude Code it is
	# a short pointer alongside the skill.
	#
	# Written whether or not the matching CLI is installed: the files are a few
	# kB, and a user who installs a harness into their own home does so long
	# after this hook has run.
	#
	# Cursor is deliberately absent. It has no user-scope instruction file at
	# all — its rules are project-scoped, and cross-project rules live in the
	# Cursor UI, not on disk. See docs/docs/guide-agentic-interface.md.
	if [[ -f "${AGENT_SECTION}" ]]; then
		for target in \
			"${NEW_HOME}/.claude/CLAUDE.md" \
			"${NEW_HOME}/.codex/AGENTS.md" \
			"${NEW_HOME}/.config/opencode/AGENTS.md"; do
			if _as_user "'${PYTHON}' /usr/local/bin/managed-block.py '${AGENT_SECTION}' '${target}'"; then
				:
			else
				echo "config-agents: WARNING could not update ${target}" >&2
			fi
		done
		chown -R "${NB_USER}:users" \
			"${NEW_HOME}/.claude" "${NEW_HOME}/.codex" "${NEW_HOME}/.config/opencode" \
			2>/dev/null || true
	else
		echo "config-agents: no bundled agent section, skipping" >&2
	fi

	return 0
}

_config_agents
