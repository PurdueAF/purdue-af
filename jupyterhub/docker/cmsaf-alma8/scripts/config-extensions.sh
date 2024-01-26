# Configure jupyterlab-unfold extension
mkdir -p /opt/conda/share/jupyter/lab/settings
echo "{\"jupyterlab-unfold:jupyterlab-unfold-settings\": {\"singleClickToUnfold\": false}}" \
 > /opt/conda/share/jupyter/lab/settings/overrides.json

# Configure topbar extension
NEW_HOME=/home/$NB_USER
TOPBAR_CONFIG_PATH=$NEW_HOME/.jupyter/lab/user-settings/@jupyterlab/application-extension/
TOPBAR_TEXT_CONFIG_PATH=$NEW_HOME/.jupyter/lab/user-settings/jupyterlab-topbar-text/

mkdir -p $TOPBAR_CONFIG_PATH
mkdir -p $TOPBAR_TEXT_CONFIG_PATH
rm -rf $NEW_HOME/.jupyter/lab/user-settings/jupyterlab-topbar-extension/

IMAGE_VERSION=${JUPYTER_IMAGE#*:}

if [ $NAMESPACE == "cms-dev" ]; then
    text="{\"text\":\"🚧 dev 🚧  |  Purdue AF v$IMAGE_VERSION  |  👤 $NB_USER  |  \"}"
else
    text="{\"text\":\"Purdue AF v$IMAGE_VERSION  |  👤 $NB_USER  |  \"}"
fi

echo  $text > $TOPBAR_TEXT_CONFIG_PATH/plugin.jupyterlab-settings

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
            "name": "logout",
            "command": "jupyterlab-topbar:logout",
            "disabled": false,
            "rank": 150
        },
        {
            "name": "shutdown",
            "command": "jupyterlab-topbar:shutdown",
            "disabled": false,
            "rank": 160
        }
    ]
}' > $TOPBAR_CONFIG_PATH/top-bar.jupyterlab-settings

chown -R $NB_USER:users $TOPBAR_TEXT_CONFIG_PATH
chown -R $NB_USER:users $TOPBAR_CONFIG_PATH

# The current environment and dask configuration via environment
# export DASK_DISTRIBUTED__DASHBOARD_LINK=/user/$NB_USER/proxy/8787/status
export DASK_GATEWAY__ADDRESS=http://dask-gateway.geddes.rcac.purdue.edu
export DASK_GATEWAY__PROXY_ADDRESS=api-dask-gateway.cms.geddes.rcac.purdue.edu:8000
export DASK_LABEXTENSION__FACTORY__CLASS=GatewayCluster
export DASK_LABEXTENSION__FACTORY__MODULE=dask_gateway

# export DASK_GATEWAY__AUTH__TYPE=jupyterhub
# export DASK_GATEWAY__CLUSTER__OPTIONS__IMAGE={JUPYTER_IMAGE_SPEC}
# export DASK_GATEWAY__PUBLIC_ADDRESS=/services/dask-gateway/
# export DASK_ROOT_CONFIG=/opt/conda/etc
