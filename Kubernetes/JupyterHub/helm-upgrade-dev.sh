helm upgrade \
    --cleanup-on-fail \
    --install cmshub jupyterhub/jupyterhub \
    --namespace cms-dev \
    --values values-dev.yaml