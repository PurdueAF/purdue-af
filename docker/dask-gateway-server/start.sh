#!/bin/bash

mkdir -p /etc/munge/
cp /etc/secrets/munge/munge.key /etc/munge/
chown munge:munge /etc/munge/munge.key
chmod 400 /etc/munge/munge.key
sudo su -l munge -s /usr/sbin/munged
dask-gateway-server --config /etc/dask-gateway/dask_gateway_config.py
