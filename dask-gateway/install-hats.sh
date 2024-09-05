helm upgrade --install dask-gateway-hats dask-gateway\
 --repo=https://helm.dask.org --install \
 --version 2024.1.0 \
 --namespace cms \
 --values config-hats.yaml \
 --post-renderer ./kustomize-hats
 