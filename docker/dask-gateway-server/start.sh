#!/bin/bash

mkdir -p /etc/munge/
install -m 400 -o munge -g munge /etc/secrets/munge/munge.key /etc/munge/munge.key
chmod 400 /etc/secrets/munge/munge.key 2>/dev/null || true
chown root:munge /etc/secrets/munge/munge.key 2>/dev/null || true
sudo su -l munge -s /usr/sbin/munged
dask-gateway-server --config /etc/dask-gateway/dask_gateway_config.py
