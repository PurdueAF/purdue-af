# This Dockerfile and image, ghcr.io/dask/dask-gateway-server, is used by the
# dask-gateway Helm chart, by the api pod and the controller pod.
#
# The pods are started with different commands:
#
# - api pod command:        dask-gateway-server ...
# - controller pod command: dask-gateway-server kube-controller ...
#
FROM python:3.11-slim-bullseye

# Set labels based on the Open Containers Initiative (OCI):
# https://github.com/opencontainers/image-spec/blob/main/annotations.md#pre-defined-annotation-keys
#
LABEL org.opencontainers.image.source="https://github.com/dask/dask-gateway"
LABEL org.opencontainers.image.url="https://github.com/dask/dask-gateway/blob/HEAD/dask-gateway-server/Dockerfile"

# Install tini and upgrade linux packages are updated to patch known
# vulnerabilities.
RUN apt-get update &&\
    apt-get upgrade -y &&\
    apt-get install -y \
        tini curl sudo alien\
        libmunge-dev libmunge2 munge libtinfo5 htop &&\
    rm -rf /var/lib/apt/lists/* &&\
    curl -LO http://launchpadlibrarian.net/354245868/libreadline7_7.0-3_amd64.deb &&\
    dpkg -i libreadline7_7.0-3_amd64.deb &&\
    rm -rf libreadline7_7.0-3_amd64.deb

# Create a non-root user to run as
RUN useradd --create-home --user-group --uid 1000 dask
USER dask:dask
ENV PATH=/home/dask/.local/bin:$PATH
WORKDIR /home/dask/

# Install Go compiler
USER root
RUN curl -LO https://golang.org/dl/go1.19.2.linux-amd64.tar.gz
RUN tar -C /usr/local -xzf go1.19.2.linux-amd64.tar.gz
# USER dask:dask

COPY slurm-21.08.8-2.el8.x86_64.rpm /etc/
COPY slurm /etc/slurm
COPY slurm/slist /usr/bin
RUN cd /etc/ &&\
    alien slurm-21.08.8-2.el8.x86_64.rpm &&\
    dpkg -i slurm_21.08.8-3_amd64.deb &&\
    rm -rf slurm-*.rpm slurm-*.deb
RUN groupadd -g 1033 slurm &&\
    adduser --uid 2000 --gid 1033 slurm &&\
    chown -R slurm:slurm /etc/slurm/ &&\
    mkdir -p /var/log/slurm &&\
    chown -R slurm:slurm /var/log/slurm

# Install dask-gateway-server
COPY --chown=dask:dask . /opt/dask-gateway-server
RUN export PATH=$PATH:/usr/local/go/bin && pip install --no-cache-dir \
        -r /opt/dask-gateway-server/Dockerfile.requirements.txt
RUN pip install sqlalchemy>=2.0.0 ldap3

RUN usermod -d /var/run/munge -s /sbin/nologin munge

ENTRYPOINT ["tini", "-g", "--"]
CMD ["/bin/sh", "-c", "/etc/init.d/munge start; dask-gateway-server --config /etc/dask-gateway/dask_gateway_config.py"]
