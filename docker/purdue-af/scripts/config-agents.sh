#!/bin/bash
# Register the AF MCP server with each agent CLI (their code-server extensions
# read the same configs), install the bundled skill, and write the platform
# context where each harness reads it. Never fatal; everything lives in a
# function that returns, since start.sh sources this file.
#
# No config stores the token: JUPYTERHUB_API_TOKEN rotates on every spawn and
# the home is persistent, so each CLI expands the variable at connect time.

_config_agents() {
	# Same name as the repo .mcp.json and jupyter_server_config.py.
	local MCP_NAME MCP_URL AUTH_HEADER SKILL_SRC AGENT_SECTION PYTHON NEW_HOME
	local OPENCODE_CFG OPENCODE_INSTRUCTIONS target
	MCP_NAME="purdue-af-agentic-interface"
	# In-cluster address: JUPYTERHUB_PUBLIC_HUB_URL is empty inside a session.
	MCP_URL="http://agentic-interface.${NAMESPACE:-cms}.svc.cluster.local:8888/services/agentic-interface/mcp"
	# Single-quoted: the placeholder reaches the config verbatim.
	AUTH_HEADER='Authorization: Bearer ${JUPYTERHUB_API_TOKEN}'
	SKILL_SRC="/opt/purdue-af/skills"
	AGENT_SECTION="/opt/purdue-af/agents/platform-context.md"
	# `su` resets PATH, and Rocky 8's system python3 is 3.6.
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

	# opencode: OPENCODE_CONFIG is a layer merged between the user's global and
	# project configs. `instructions` is its only platform-context channel: a
	# project AGENTS.md would shadow a global one.
	OPENCODE_INSTRUCTIONS=""
	if [[ -f "${AGENT_SECTION}" ]]; then
		OPENCODE_INSTRUCTIONS="\"instructions\": [\"${AGENT_SECTION}\"],"
	fi
	OPENCODE_CFG="${NEW_HOME}/.config/opencode/purdue-af.json"
	# As the user: ~/.config may be a user-planted symlink.
	# `permission` restates jupyter-ai's persona defaults, which it only applies
	# when OPENCODE_CONFIG is unset. Unquoted heredoc: ${MCP_URL} expands, \$schema not.
	if _as_user "mkdir -p '${NEW_HOME}/.config/opencode' && cat >'${OPENCODE_CFG}'" <<-JSON
		{
		  "\$schema": "https://opencode.ai/config.json",
		  ${OPENCODE_INSTRUCTIONS}
		  "permission": {
		    "edit": "ask",
		    "bash": "ask"
		  },
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
		# Reaches the server via start.sh's `sudo --preserve-env`.
		export OPENCODE_CONFIG="${OPENCODE_CFG}"
		echo "config-agents: registered '${MCP_NAME}' with opencode"
	else
		echo "config-agents: WARNING could not write ${OPENCODE_CFG}" >&2
	fi

	# Claude Code skills, prepared at build time by prepare-skill.py.
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

	# Each harness's user-scope instruction file; opencode uses `instructions`
	# above. Written even when the CLI is absent.
	if [[ -f "${AGENT_SECTION}" ]]; then
		for target in "${NEW_HOME}/.claude/CLAUDE.md" "${NEW_HOME}/.codex/AGENTS.md"; do
			if _as_user "'${PYTHON}' /usr/local/bin/managed-block.py '${AGENT_SECTION}' '${target}'"; then
				:
			else
				echo "config-agents: WARNING could not update ${target}" >&2
			fi
		done
		# The skill copy above runs as root; ~/.codex may be root-owned too.
		chown -R "${NB_USER}:users" "${NEW_HOME}/.claude" "${NEW_HOME}/.codex" \
			2>/dev/null || true
	else
		echo "config-agents: no bundled agent section, skipping" >&2
	fi

	# The AF block must not also sit in opencode's AGENTS.md: it would load twice.
	_as_user "'${PYTHON}' /usr/local/bin/managed-block.py --remove '${NEW_HOME}/.config/opencode/AGENTS.md'" ||
		echo "config-agents: WARNING could not retire ${NEW_HOME}/.config/opencode/AGENTS.md" >&2

	return 0
}

_config_agents
