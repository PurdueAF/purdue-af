# earlier: /opt/conda
base_env_dir=/opt/pixi/.pixi/envs/base-env/

# Configure jupyterlab-unfold extension
mkdir -p $base_env_dir/share/jupyter/lab/settings
echo "{\"jupyterlab-unfold:jupyterlab-unfold-settings\": {\"singleClickToUnfold\": false}}" \
	$base_env_dir/share/jupyter/lab/settings/overrides.json

# Configure topbar extension
NEW_HOME=/home/$NB_USER
TOPBAR_CONFIG_PATH=$NEW_HOME/.jupyter/lab/user-settings/@jupyterlab/application-extension/
TOPBAR_TEXT_CONFIG_PATH=$NEW_HOME/.jupyter/lab/user-settings/jupyterlab-topbar-text/

mkdir -p $TOPBAR_CONFIG_PATH
mkdir -p $TOPBAR_TEXT_CONFIG_PATH
rm -rf $NEW_HOME/.jupyter/lab/user-settings/jupyterlab-topbar-extension/

IMAGE_VERSION=${JUPYTER_IMAGE#*:}

if [ $NAMESPACE == "cms-dev" ]; then
	text='{"text":"🚧 dev 🚧  |  Purdue AF v'"$IMAGE_VERSION"'  |  👤 '"$NB_USER"'  |  "}'
else
	text='{"text":"Purdue AF v'"$IMAGE_VERSION"'  |  👤 '"$NB_USER"'  |  "}'
fi

echo $text >$TOPBAR_TEXT_CONFIG_PATH/plugin.jupyterlab-settings

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
            "name": "memory",
            "command": "",
            "disabled": false,
            "rank": 120
        },
        {
            "name": "theme-toggler",
            "command": "",
            "disabled": false,
            "rank": 130
        },
        {
            "name": "shutdown",
            "command": "jupyterlab-topbar:shutdown",
            "disabled": false,
            "rank": 160
        }
    ]
}' >$TOPBAR_CONFIG_PATH/top-bar.jupyterlab-settings

chown -R $NB_USER:users $TOPBAR_TEXT_CONFIG_PATH
chown -R $NB_USER:users $TOPBAR_CONFIG_PATH

JIL_PATH=$NEW_HOME/.jupyter/lab/user-settings/purdue-af-grafana-iframe/
mkdir -p $JIL_PATH
DASHBOARD_URL="https://cms.geddes.rcac.purdue.edu/grafana/d-solo/single-user-stat-dashboard/single-user-statistics"
THEME="&theme=light"
echo "{
    \"url\": \"$DASHBOARD_URL?orgId=1&refresh=1m&var-user=$HOSTNAME&from=now-3h&to=now&panelId=1$THEME\",
    \"label\": \"Resource usage\",
    \"caption\": \"Open grafana panel\",
    \"rank\": 0
}" >$JIL_PATH/plugin.jupyterlab-settings

# Configure prometheus alerts extension
mkdir -p $base_env_dir/etc/jupyter/jupyter_server_config.d
echo '{
  "ServerApp": {
    "jpserver_extensions": {
      "prometheus_alerts": true
    }
  }
}' >$base_env_dir/etc/jupyter/jupyter_server_config.d/prometheus_alerts.json

# Pre-install code-server extensions into the notebook user's dirs; fail if CLI is unavailable
CODE_SERVER_BIN="${base_env_dir%/}/bin/code-server"
if [ ! -x "$CODE_SERVER_BIN" ]; then
	echo "ERROR: code-server CLI not found or not executable at $CODE_SERVER_BIN" >&2
	exit 1
fi

export CODE_EXTENSIONSDIR="$NEW_HOME/.local/share/code-server/extensions"
export CODE_USERDATADIR="$NEW_HOME/.local/share/code-server"
mkdir -p "$CODE_EXTENSIONSDIR" "$CODE_USERDATADIR"

# Disable default GitHub chat in code-server
CODE_SERVER_USER_SETTINGS="$CODE_USERDATADIR/User"
mkdir -p "$CODE_SERVER_USER_SETTINGS"
echo '{
  "chat.disableAIFeatures": true,
  "chat.commandCenter.enabled": false
}' >"$CODE_SERVER_USER_SETTINGS/settings.json"

chown -R $NB_USER:users "$CODE_EXTENSIONSDIR" "$CODE_USERDATADIR"
"$CODE_SERVER_BIN" --extensions-dir "$CODE_EXTENSIONSDIR" --user-data-dir "$CODE_USERDATADIR" --install-extension ms-python.python
"$CODE_SERVER_BIN" --extensions-dir "$CODE_EXTENSIONSDIR" --user-data-dir "$CODE_USERDATADIR" --install-extension ms-toolsai.jupyter
"$CODE_SERVER_BIN" --extensions-dir "$CODE_EXTENSIONSDIR" --user-data-dir "$CODE_USERDATADIR" --install-extension continue.continue@1.3.30
chown -R $NB_USER:users "$CODE_EXTENSIONSDIR" "$CODE_USERDATADIR"

# Continue extension config (from bundled file)
CONTINUE_DIR="$NEW_HOME/.continue"
mkdir -p "$CONTINUE_DIR"
cp /etc/jupyter/continue-config.yaml "$CONTINUE_DIR/config.yaml"
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
		mv "$tmp" "$CONTINUE_DIR/config.yaml"
	fi
fi
chown -R $NB_USER:users "$CONTINUE_DIR"
