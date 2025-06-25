import os
import time

import dask_gateway
from dask_gateway import Gateway

gateway = Gateway(
    "http://dask-gateway-k8s.geddes.rcac.purdue.edu/",
    proxy_address="traefik-dask-gateway-k8s.cms.geddes.rcac.purdue.edu:8786",
)

cluster = gateway.new_cluster(
    conda_env="/depot/cms/kernels/python3",  # path to conda env
    worker_cores=1,  # cores per worker
    worker_memory=4,  # memory per worker in GB
    env=dict(os.environ),  # pass environment as a dictionary
)
time.sleep(600)
