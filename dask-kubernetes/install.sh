helm install --repo https://helm.dask.org -n cms --generate-name dask-kubernetes-operator \
  --set rbac.cluster=false --set kopfArgs="{--namespace=cms}"
