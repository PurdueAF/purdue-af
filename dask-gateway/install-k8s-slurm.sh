# helm
helm upgrade  dask-gateway-k8s-slurm dask-gateway\
 --repo=https://helm.dask.org/ --install \
 --version 2023.9.0 \
 --namespace cms \
 --values config-k8s-slurm.yaml \
 --post-renderer ./kustomize-k8s-slurm