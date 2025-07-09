# This Dockerfile and image, ghcr.io/dask/dask-gateway-server, is used by the
# dask-gateway Helm chart, by the api pod and the controller pod.
#
# The pods are started with different commands:
#
# - api pod command:        dask-gateway-server ...
# - controller pod command: dask-gateway-server kube-controller ...
#
FROM gitlab-registry.cern.ch/linuxsupport/alma8-base:latest

# Set labels based on the Open Containers Initiative (OCI):
# https://github.com/opencontainers/image-spec/blob/main/annotations.md#pre-defined-annotation-keys
#
LABEL org.opencontainers.image.source="https://github.com/dask/dask-gateway"
LABEL org.opencontainers.image.url="https://github.com/dask/dask-gateway/blob/HEAD/dask-gateway-server/Dockerfile"

# Install tini and upgrade linux packages are updated to patch known
# vulnerabilities.
RUN yum update -y && \
    yum upgrade -y && \
    yum install -y \
        https://dl.fedoraproject.org/pub/epel/epel-release-latest-8.noarch.rpm \
        https://repo.opensciencegrid.org/osg/3.6/osg-3.6-el8-release-latest.rpm \
        curl sudo libXft libXpm libXext libSM mesa-libGL glibc-locale-source glibc-langpack-en\
        wget bzip2 &&\
    curl -O https://dl.fedoraproject.org/pub/epel/8/Everything/x86_64/Packages/t/tini-0.19.0-1.el8.x86_64.rpm && \
    yum install -y tini-0.19.0-1.el8.x86_64.rpm &&\
    yum clean all && rm -rf /var/lib/apt/lists/* &&\
    localedef -c -f UTF-8 -i en_US en_US.UTF-8 && \
    export LC_ALL=en_US.UTF-8

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

COPY slurm/slurm-24.05.1-1.el8.x86_64.rpm /etc/
COPY slurm/slurm-configs /etc/slurm
RUN dnf localinstall -y /etc/slurm-24.05.1-1.el8.x86_64.rpm && \
    rm -rf /etc/slurm-24.05.1-1.el8.x86_64.rpm && \
    groupadd -g 1033 slurm && \
    adduser -u 2000 -g 1033 slurm && \
    chown -R slurm:slurm /etc/slurm/ && \
    mkdir -p /var/log/slurm && \
    chown -R slurm:slurm /var/log/slurm

# Install dask-gateway-server
# RUN dnf install -y gcc openssl-devel bzip2-devel libffi-devel zlib-devel wget make
# RUN curl -LO https://www.python.org/ftp/python/3.10.0/Python-3.10.0.tar.xz &&\
#     tar -xf Python-3.10.0.tar.xz &&\
#     cd Python-3.10.0 && ./configure --enable-optimizations &&\
#     make -j 2 &&\
#     nproc &&\
#     make altinstall
ENV CONDA_DIR=/opt/conda
ENV PATH="${CONDA_DIR}/bin:${PATH}"
# RUN sed -i 's/^#force_color_prompt=yes/force_color_prompt=yes/' /etc/skel/.bashrc && \
#    # Add call to conda init script see https://stackoverflow.com/a/58081608/4413446
#    echo 'eval "$(command conda shell.bash hook 2> /dev/null)"' >> /etc/skel/.bashrc

# RUN wget -qO /tmp/micromamba.tar.bz2 "https://micromamba.snakepit.net/api/micromamba/linux-64/latest" 
RUN curl -sL -o /tmp/micromamba.tar.bz2 "https://micromamba.snakepit.net/api/micromamba/linux-64/latest" && \
    tar -xvjf /tmp/micromamba.tar.bz2 --strip-components=1 bin/micromamba && \
    rm /tmp/micromamba.tar.bz2 && \
    # Install the packages
    ./micromamba config append channels conda-forge &&\
    ./micromamba install \
        --root-prefix="${CONDA_DIR}" \
        --prefix="${CONDA_DIR}" \
        --yes \
        "python=3.10" \
        'mamba' &&\
    rm micromamba &&\
    # # Pin major.minor version of python
    mamba list python | grep '^python ' | tr -s ' ' | cut -d ' ' -f 1,2 >> "${CONDA_DIR}/conda-meta/pinned" && \
    mamba clean --all -f -y 

COPY --chown=dask:dask docker/dask-gateway-server/ /opt/dask-gateway-server/
RUN export PATH=$PATH:/usr/local/go/bin && pip install --no-cache-dir \
        -r /opt/dask-gateway-server/Dockerfile.requirements.txt
RUN pip install sqlalchemy>=2.0.0 ldap3

ADD docker/dask-gateway-server/start.sh /
RUN chmod +x /start.sh

ENTRYPOINT ["tini", "-g", "--"]
CMD ["/start.sh"]
