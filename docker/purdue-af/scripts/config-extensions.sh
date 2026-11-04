#!/bin/bash
# Configure topbar extension
NEW_HOME=/home/$NB_USER

# Every write below lands under $NEW_HOME, so it is done as the user.
source /usr/local/bin/af-as-user.sh

TOPBAR_CONFIG_PATH=$NEW_HOME/.jupyter/lab/user-settings/@jupyterlab/application-extension/
TOPBAR_TEXT_CONFIG_PATH=$NEW_HOME/.jupyter/lab/user-settings/jupyterlab-topbar-text/

chown -Rh "$NB_USER:users" "$NEW_HOME/.jupyter/lab/user-settings" 2>/dev/null || true
af_as_user mkdir -p $TOPBAR_CONFIG_PATH
af_as_user mkdir -p $TOPBAR_TEXT_CONFIG_PATH

IMAGE_VERSION=${JUPYTER_IMAGE#*:}

text='{"text":"Purdue AF v'"$IMAGE_VERSION"'  |  👤 '"$NB_USER"'  |  "}'

echo "$text" | af_as_user tee "$TOPBAR_TEXT_CONFIG_PATH/plugin.jupyterlab-settings" >/dev/null

echo '{
    "toolbar": [
        {
            "name": "spacer",
            "command": "",
            "disabled": false,
            "type": "spacer",
            "rank": 50
        },
        {
            "name": "text",
            "command": "",
            "disabled": false,
            "rank": 110
        },
        {
            "name": "theme-toggler",
            "command": "",
            "disabled": false,
            "rank": 130
        },
        {
            "name": "vscode",
            "command": "jupyterlab-topbar:switch-to-vscode",
            "disabled": false,
            "rank": 160
        },
        {
            "name": "shutdown",
            "command": "jupyterlab-topbar:shutdown",
            "disabled": false,
            "rank": 170
        }
    ]
}' | af_as_user tee $TOPBAR_CONFIG_PATH/top-bar.jupyterlab-settings >/dev/null

CODE_SERVER_BIN=/opt/pixi/.pixi/envs/base-env/bin/code-server
# A broken code-server skips the editor setup, never the session.
code_server_ok=1
if [ ! -x "$CODE_SERVER_BIN" ]; then
	echo "ERROR: code-server CLI not found or not executable at $CODE_SERVER_BIN" >&2
	echo "ERROR: skipping code-server setup; JupyterLab is unaffected" >&2
	code_server_ok=0
fi

if [ "$code_server_ok" = 1 ]; then

	export CODE_EXTENSIONSDIR="$NEW_HOME/.local/share/code-server/extensions"
	export CODE_USERDATADIR="$NEW_HOME/.local/share/code-server"
	CODE_SERVER_USER_SETTINGS="$CODE_USERDATADIR/User"

	af_repair_ownership "$CODE_USERDATADIR" "$CODE_EXTENSIONSDIR"

	# Fails only on a full quota or dangling symlink: skip the editor then.
	if ! af_as_user mkdir -p \
		"$CODE_EXTENSIONSDIR" "$CODE_USERDATADIR" "$CODE_SERVER_USER_SETTINGS"; then
		echo "ERROR: cannot create code-server directories under $NEW_HOME/.local" >&2
		echo "ERROR: skipping code-server setup; JupyterLab is unaffected" >&2
		code_server_ok=0
	fi
fi

if [ "$code_server_ok" = 1 ]; then

	# Disable default GitHub chat in code-server
	HUB_PREFIX="${JUPYTERHUB_SERVICE_PREFIX:-/user/${NB_USER}/}"
	LAB_PATH="${HUB_PREFIX%/}/lab"
	HUB_HOME_PATH="/hub/home"
	HUB_ORIGIN=""
	if [[ "${JUPYTERHUB_BASE_URL:-}" == http* ]]; then
		HUB_ORIGIN="${JUPYTERHUB_BASE_URL%/}"
	fi
	# `af_as_user tee`, not a redirect: root opens redirects.
	af_as_user tee "$CODE_SERVER_USER_SETTINGS/settings.json" >/dev/null <<EOF
{
  "chat.disableAIFeatures": true,
  "chat.commandCenter.enabled": false,
  "window.autoDetectColorScheme": true,
  "window.menuBarVisibility": "classic",
  "files.exclude": {
    ".*": true,
    "~*": true
  },
  "purdueaf.jupyterLabPath": "${LAB_PATH}",
  "purdueaf.hubHomePath": "${HUB_HOME_PATH}",
  "purdueaf.servicePrefix": "${HUB_PREFIX}",
  "purdueaf.hubOrigin": "${HUB_ORIGIN}"
}
EOF

	# Install when missing, or when a pinned version differs from what is there.
	# Skipping avoids ~3 s CLI overhead per extension on warm starts.
	_cs_install_if_missing() {
		local spec="$1"
		local id="${spec%@*}"
		local want=""
		if [ "$spec" != "$id" ]; then
			want="${spec#*@}"
		fi

		local installed have
		installed=$(af_as_user "$CODE_SERVER_BIN" --extensions-dir "$CODE_EXTENSIONSDIR" --user-data-dir "$CODE_USERDATADIR" \
			--list-extensions --show-versions 2>/dev/null | grep -i "^${id}@" | head -1)
		have="${installed#*@}"

		if [ -n "$installed" ] && { [ -z "$want" ] || [ "$have" = "$want" ]; }; then
			echo "code-server extension '${spec}' already installed, skipping."
			return 0
		fi
		if [ -n "$installed" ]; then
			echo "code-server extension '${id}' is ${have}, want ${want}; reinstalling."
		fi

		af_as_user "$CODE_SERVER_BIN" --extensions-dir "$CODE_EXTENSIONSDIR" --user-data-dir "$CODE_USERDATADIR" \
			--install-extension "$spec" --force
	}

	_cs_install_if_missing ms-python.python
	_cs_install_if_missing ms-toolsai.jupyter
	_cs_install_if_missing renan-r-santos.pixi-code
	# code-server ships no PDF viewer.
	_cs_install_if_missing mathematic.vscode-pdf
	# Open VSX IDs (code-server's marketplace).
	_cs_install_if_missing anthropic.claude-code
	# Pinned: newer builds emit `using` declarations code-server's Node cannot parse.
	_cs_install_if_missing openai.chatgpt@26.820.71523

	# The bundled extension: only when the image's build differs from the installed
	# one. Uninstalling first also clears a registration whose directory is gone,
	# which --force alone refuses.
	PAF_CS_EXT_VSIX="/opt/purdue-af/code-server/purdue-af-interface-controls.vsix"
	PAF_CS_EXT_ID="purdueaf.purdue-af-interface-controls"
	PAF_CS_EXT_STAMP="$CODE_EXTENSIONSDIR/.purdue-af-interface-controls.sha256"

	_cs_bundled_is_current() {
		af_as_user bash -c 'cmp -s "$1" "$2" &&
			grep -qF "\"$3\"" "$4/extensions.json" &&
			compgen -G "$4/$3-*" >/dev/null' _ \
			"$PAF_CS_EXT_VSIX.sha256" "$PAF_CS_EXT_STAMP" "$PAF_CS_EXT_ID" "$CODE_EXTENSIONSDIR"
	}

	if [ ! -f "$PAF_CS_EXT_VSIX" ]; then
		echo "WARNING: bundled Purdue AF code-server VSIX not found at ${PAF_CS_EXT_VSIX}" >&2
	elif _cs_bundled_is_current; then
		echo "Purdue AF code-server extension is current, skipping."
	else
		af_as_user "$CODE_SERVER_BIN" --extensions-dir "$CODE_EXTENSIONSDIR" --user-data-dir "$CODE_USERDATADIR" \
			--uninstall-extension "$PAF_CS_EXT_ID" >/dev/null 2>&1 || true
		if af_as_user "$CODE_SERVER_BIN" --extensions-dir "$CODE_EXTENSIONSDIR" --user-data-dir "$CODE_USERDATADIR" \
			--install-extension "$PAF_CS_EXT_VSIX" --force; then
			af_as_user cp "$PAF_CS_EXT_VSIX.sha256" "$PAF_CS_EXT_STAMP" || true
			echo "Installed Purdue AF code-server extension from ${PAF_CS_EXT_VSIX}"
		else
			echo "ERROR: could not install ${PAF_CS_EXT_VSIX}" >&2
		fi
	fi

fi

# A home that still holds the Continue extension loses it, with the config and the key it stored.
_retire_continue() {
	local extensions="$NEW_HOME/.local/share/code-server/extensions" dir="$NEW_HOME/.continue"
	if compgen -G "$extensions/continue.continue-*" >/dev/null && [ -x "$CODE_SERVER_BIN" ]; then
		af_as_user "$CODE_SERVER_BIN" --extensions-dir "$extensions" \
			--user-data-dir "$NEW_HOME/.local/share/code-server" \
			--uninstall-extension continue.continue >/dev/null 2>&1 || true
	fi
	af_as_user rm -f "$dir/config.yaml" "$dir/api-key.txt" || true
	af_as_user rmdir "$dir" 2>/dev/null || true
}
_retire_continue
