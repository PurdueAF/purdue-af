helm upgrade --cleanup-on-fail --install cmshub jupyterhub/jupyterhub \
    --namespace cms-dev --values values-dev.yaml \
    --set-file hub.extraFiles.00-custom-spawner.stringData=extra_config/custom-spawner.py \
    --set-file hub.extraFiles.01-set-user-info.stringData=extra_config/set-user-info-dev.py
