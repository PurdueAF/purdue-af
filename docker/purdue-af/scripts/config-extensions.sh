#!/bin/bash
# earlier: /opt/conda
base_env_dir=/opt/pixi/.pixi/envs/base-env/

# Configure JupyterLab overrides (single-click unfold, disable PyPI extension manager)
mkdir -p $base_env_dir/share/jupyter/lab/settings
cat >$base_env_dir/share/jupyter/lab/settings/overrides.json <<'OVERRIDES_EOF'
{
  "jupyterlab-unfold:jupyterlab-unfold-settings": {
    "singleClickToUnfold": false
  },
  "@jupyterlab/extensionmanager-extension:plugin": {
    "enabled": false
  }
}
OVERRIDES_EOF

# Configure topbar extension
NEW_HOME=/home/$NB_USER

# Every write below lands under $NEW_HOME, which belongs to the user, so it is
# done as the user. See the helper for why that matters.
source /usr/local/bin/af-as-user.sh

TOPBAR_CONFIG_PATH=$NEW_HOME/.jupyter/lab/user-settings/@jupyterlab/application-extension/
TOPBAR_TEXT_CONFIG_PATH=$NEW_HOME/.jupyter/lab/user-settings/jupyterlab-topbar-text/

af_as_user mkdir -p $TOPBAR_CONFIG_PATH
af_as_user mkdir -p $TOPBAR_TEXT_CONFIG_PATH
af_as_user rm -rf $NEW_HOME/.jupyter/lab/user-settings/jupyterlab-topbar-extension/

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

JIL_PATH=$NEW_HOME/.jupyter/lab/user-settings/purdue-af-grafana-iframe/
af_as_user mkdir -p $JIL_PATH
DASHBOARD_URL="https://cms.geddes.rcac.purdue.edu/grafana/d-solo/single-user-stat-dashboard/single-user-statistics"
THEME="&theme=light"
echo "{
    \"url\": \"$DASHBOARD_URL?orgId=1&refresh=1m&var-user=$HOSTNAME&from=now-3h&to=now&panelId=1$THEME\",
    \"label\": \"Resource usage\",
    \"caption\": \"Open grafana panel\",
    \"rank\": 0
}" | af_as_user tee $JIL_PATH/plugin.jupyterlab-settings >/dev/null

# Pre-install code-server extensions into the notebook user's dirs; fail if CLI is unavailable
CODE_SERVER_BIN="${base_env_dir%/}/bin/code-server"
# start.sh SOURCES this file, so `exit` here would kill the container before
# JupyterLab launches. A broken code-server must not cost the user their whole
# session — skip the editor setup and let the session come up without it.
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

	# Backstop only: as the user this succeeds wherever ~/.local actually lives.
	# If it still fails — a full quota, a dangling symlink — the session must
	# survive it, so skip the editor rather than exiting the hook.
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
	# `af_as_user tee` rather than a plain redirect: the redirect is opened by
	# this shell, which is root, so it would fail on a depot-backed ~/.local
	# exactly as the mkdir did.
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
  "continue.enableNextEdit": false,
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
	_cs_install_if_missing continue.continue@1.3.30
	_cs_install_if_missing renan-r-santos.pixi-code
	# Coding agents. Both extensions drive the CLIs installed in the image and
	# read the same config files, so config-agents.sh registers the AF MCP server
	# for the editor and the terminal in one go. Open VSX is code-server's
	# marketplace — these IDs are the Open VSX ones, not the MS Marketplace ones.
	_cs_install_if_missing anthropic.claude-code
	# Pinned: 26.901.22334 emits `using` declarations, which the Node 22 bundled
	# with code-server cannot parse, so the extension never activates.
	_cs_install_if_missing openai.chatgpt@26.820.71523

	# Install Purdue AF code-server UI controls via VSIX (proper extensions.json registration)
	PAF_CS_EXT_VSIX="/opt/purdue-af/code-server/purdue-af-interface-controls.vsix"
	PAF_CS_EXT_ID="purdueaf.purdue-af-interface-controls"

	_cs_clear_extension_state() {
		local ext_id="$1"
		af_as_user python - "$CODE_EXTENSIONSDIR" "$ext_id" <<'PY'
import glob
import json
import os
import sys

extensions_dir, ext_id = sys.argv[1], sys.argv[2].lower()

for path in glob.glob(os.path.join(extensions_dir, f"{ext_id}-*")):
    print(f"Removing stale extension directory: {path}")
    import shutil
    shutil.rmtree(path, ignore_errors=True)

meta_path = os.path.join(extensions_dir, "extensions.json")
if os.path.isfile(meta_path):
    try:
        with open(meta_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        data = None
    if isinstance(data, list):
        filtered = [
            item for item in data
            if str(((item.get("identifier") or {}).get("id") or "")).lower() != ext_id
        ]
        if filtered != data:
            with open(meta_path, "w", encoding="utf-8") as handle:
                json.dump(filtered, handle)

obsolete_path = os.path.join(extensions_dir, ".obsolete")
if os.path.isfile(obsolete_path):
    try:
        with open(obsolete_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        data = None
    if isinstance(data, dict):
        stale = [key for key in data if key.lower().startswith(ext_id)]
        for key in stale:
            print(f"Removing stale obsolete entry: {key}")
            data.pop(key, None)
        with open(obsolete_path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
PY
	}

	if [ -f "$PAF_CS_EXT_VSIX" ]; then
		_cs_clear_extension_state "$PAF_CS_EXT_ID"
		af_as_user "$CODE_SERVER_BIN" --extensions-dir "$CODE_EXTENSIONSDIR" --user-data-dir "$CODE_USERDATADIR" \
			--uninstall-extension "$PAF_CS_EXT_ID" >/dev/null 2>&1 || true
		af_as_user "$CODE_SERVER_BIN" --extensions-dir "$CODE_EXTENSIONSDIR" --user-data-dir "$CODE_USERDATADIR" \
			--install-extension "$PAF_CS_EXT_VSIX"
		echo "Installed Purdue AF code-server extension from ${PAF_CS_EXT_VSIX}"
	else
		echo "WARNING: bundled Purdue AF code-server VSIX not found at ${PAF_CS_EXT_VSIX}" >&2
	fi

	# Everything above was created as the user, so this only repairs homes an
	# earlier image left root-owned. It cannot work on a depot-backed ~/.local
	# (root_squash again) and must not be fatal there.
	chown -R $NB_USER:users "$CODE_EXTENSIONSDIR" "$CODE_USERDATADIR" || true

fi

# Continue extension config (from bundled file)
CONTINUE_DIR="$NEW_HOME/.continue"
af_as_user mkdir -p "$CONTINUE_DIR"
# Read the bundled file as root (it lives in the image), write it as the user.
af_as_user tee "$CONTINUE_DIR/config.yaml" </etc/jupyter/continue-config.yaml >/dev/null
# If user previously saved an API key, inject it into all apiKey fields so config survives image startup
if [[ -s "$CONTINUE_DIR/api-key.txt" ]]; then
	KEY=$(tr -d '\n\r' <"$CONTINUE_DIR/api-key.txt")
	if [[ -n "$KEY" ]]; then
		tmp=$(mktemp)
		while IFS= read -r line; do
			if [[ "$line" =~ ^([[:space:]]*apiKey:)[[:space:]]*(.*)$ ]]; then
				printf '%s %s\n' "${BASH_REMATCH[1]}" "$KEY"
			else
				printf '%s\n' "$line"
			fi
		done <"$CONTINUE_DIR/config.yaml" >"$tmp"
		# Not `mv`: the rename would be done by root, and the key travels on
		# stdin rather than in argv, where `ps` would show it.
		af_as_user tee "$CONTINUE_DIR/config.yaml" <"$tmp" >/dev/null
		rm -f "$tmp"
	fi
fi
# Only repairs homes an earlier image left root-owned; must not be fatal.
chown -R $NB_USER:users "$CONTINUE_DIR" || true
