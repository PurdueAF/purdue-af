#!/bin/bash
# What the image cannot hold for the agent CLIs: the AF MCP server in Claude
# Code's user config and the platform context in Codex's instruction file; the
# home's copies of what the image does hold are removed. Never fatal; everything
# lives in a function that returns, since start.sh sources this file.

source /usr/local/bin/af-as-user.sh

_config_agents() {
	# Same name as the repo .mcp.json and jupyter_server_config.py.
	local MCP_NAME MCP_URL AUTH_HEADER AGENT_SECTION PYTHON NEW_HOME
	MCP_NAME="purdue-af-agentic-interface"
	# In-cluster address: JUPYTERHUB_PUBLIC_HUB_URL is empty inside a session.
	MCP_URL="http://agentic-interface.${NAMESPACE:-cms}.svc.cluster.local:8888/services/agentic-interface/mcp"
	# Single-quoted: the placeholder reaches the config verbatim, since the token rotates on every spawn.
	AUTH_HEADER='Authorization: Bearer ${JUPYTERHUB_API_TOKEN}'
	AGENT_SECTION="/opt/purdue-af/agents/platform-context.md"
	# Rocky 8's system python3 is 3.6.
	PYTHON="/opt/pixi/.pixi/envs/base-env/bin/python3"
	[[ -x "${PYTHON}" ]] || PYTHON="python3"

	if [[ -z "${NB_USER:-}" ]]; then
		echo "config-agents: NB_USER unset, skipping" >&2
		return 0
	fi
	NEW_HOME="/home/${NB_USER}"

	# The startup hooks run as root; drop to the session user so the configs land
	# in their home with their ownership.
	_as_user() { af_as_user bash -c "$1"; }

	af_repair_ownership "${NEW_HOME}/.claude" "${NEW_HOME}/.codex"

	# Claude Code's system-level MCP entries lock out a user's own servers or expand no
	# variable, so this one goes into ~/.claude.json; `mcp add` is not idempotent.
	if command -v claude >/dev/null 2>&1; then
		_as_user "claude mcp remove --scope user '${MCP_NAME}'" >/dev/null 2>&1 || true
		if _as_user "claude mcp add --scope user --transport http '${MCP_NAME}' '${MCP_URL}' --header '${AUTH_HEADER}'" >/dev/null 2>&1; then
			echo "config-agents: registered '${MCP_NAME}' with claude"
		else
			echo "config-agents: WARNING could not register MCP server with claude" >&2
		fi
	else
		echo "config-agents: claude CLI not found, skipping" >&2
	fi

	# Codex has no system-level instruction file.
	if [[ -f "${AGENT_SECTION}" ]]; then
		if ! _as_user "'${PYTHON}' /usr/local/bin/managed-block.py '${AGENT_SECTION}' '${NEW_HOME}/.codex/AGENTS.md'"; then
			echo "config-agents: WARNING could not update ${NEW_HOME}/.codex/AGENTS.md" >&2
		fi
	else
		echo "config-agents: no bundled agent section, skipping" >&2
	fi

	# What /etc/claude-code, /etc/codex and OPENCODE_CONFIG hold, where a home still has a copy.
	_as_user "'${PYTHON}' /usr/local/bin/managed-block.py --remove '${NEW_HOME}/.claude/CLAUDE.md'" || true
	_as_user "rm -f '${NEW_HOME}/.claude/skills/${MCP_NAME}/SKILL.md' '${NEW_HOME}/.config/opencode/purdue-af.json'" || true
	_as_user "rmdir '${NEW_HOME}/.claude/skills/${MCP_NAME}'" 2>/dev/null || true
	if _as_user "grep -qsF '[mcp_servers.${MCP_NAME}]' '${NEW_HOME}/.codex/config.toml'"; then
		_as_user "codex mcp remove '${MCP_NAME}'" >/dev/null 2>&1 || true
	fi

	return 0
}

_config_agents
