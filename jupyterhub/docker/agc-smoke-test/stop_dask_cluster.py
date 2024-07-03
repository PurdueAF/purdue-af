import os
import dask_gateway
from dask_gateway import Gateway

gateway = Gateway(
    "http://dask-gateway-k8s.geddes.rcac.purdue.edu/",
    proxy_address="traefik-dask-gateway-k8s.cms.geddes.rcac.purdue.edu:8786",
)

for cluster_info in gateway.list_clusters():
    gateway.connect(cluster_info.name).shutdown()