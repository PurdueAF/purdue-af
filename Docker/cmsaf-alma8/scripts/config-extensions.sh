# Configure jupyterlab-unfold extension
echo "{\"jupyterlab-unfold:jupyterlab-unfold-settings\": {\"singleClickToUnfold\": false}}" \
 > /opt/conda/share/jupyter/lab/settings/overrides.json

# Configure topbar extension
NEW_HOME=/home/$NB_USER
TOPBAR_EXTENSION_PATH="$NEW_HOME/.jupyter/lab/user-settings/jupyterlab-topbar-extension/"
TOPBAR_TEXT_CONFIG_PATH=$NEW_HOME/.jupyter/lab/user-settings/jupyterlab-topbar-text/
IMAGE_VERSION=$(basename "$JUPYTER_IMAGE")

mkdir -p $TOPBAR_EXTENSION_PATH
mkdir -p $TOPBAR_TEXT_CONFIG_PATH

echo "{ \"order\": [ \"spacer\", \"custom-text\", \"theme-toggle\", \"logout-button\" ] }" \
 > $TOPBAR_EXTENSION_PATH/plugin.jupyterlab-settings;

if [ $NAMESPACE == "cms-dev" ]; then
    text="{\"text\":\"Purdue AF: 🚧 Developers area 🚧 \| User: $NB_USER \| Release: $IMAGE_VERSION\"}"
else
    text="{\"text\":\"Purdue Analysis Facility \| User: $NB_USER \| Release: $IMAGE_VERSION\"}"
fi

echo  $text > $TOPBAR_TEXT_CONFIG_PATH/plugin.jupyterlab-settings

chown -R $NB_USER:users $TOPBAR_EXTENSION_PATH
chown -R $NB_USER:users $TOPBAR_TEXT_CONFIG_PATH
