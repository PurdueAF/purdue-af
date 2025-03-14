helm upgrade --cleanup-on-fail --install cmshub jupyterhub/jupyterhub --version 3.2.1 \
    --namespace cms --values values.yaml \
    --set-file hub.extraFiles.00-custom-spawner.stringData=extra_config/custom-spawner.py \
    --set-file hub.extraFiles.01-set-user-info.stringData=extra_config/set-user-info.py
