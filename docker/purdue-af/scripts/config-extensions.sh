# earlier: /opt/conda
base_env_dir=/opt/pixi/envs/base-env/

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
