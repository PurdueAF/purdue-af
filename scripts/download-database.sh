#!/bin/bash
POD="hub-c754659d5-t8dp4"
NAMESPACE="cms"
REMOTE_DB_PATH="/srv/jupyterhub/jupyterhub.sqlite"
LOCAL_DB_PATH="./jupyterhub.sqlite"

kubectl cp ${NAMESPACE}/${POD}:${REMOTE_DB_PATH} ${LOCAL_DB_PATH}

echo "Database has been downloaded to ${LOCAL_DB_PATH}"
