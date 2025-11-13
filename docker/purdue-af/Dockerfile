# Copyright (c) Jupyter Development Team.
# Distributed under the terms of the Modified BSD License.
ARG OWNER=jupyter
FROM gitlab-registry.cern.ch/linuxsupport/alma8-base:20250501-1

LABEL maintainer="Dmitry Kondratyev <dkondra@purdue.edu>"
ARG NB_USER="jovyan"
ARG NB_UID="1000"
ARG NB_GID="100"

# Fix: https://github.com/hadolint/hadolint/wiki/DL4006
# Fix: https://github.com/koalaman/shellcheck/wiki/SC3014
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

USER root

RUN set -euo pipefail \
 && dnf -y upgrade \
 && dnf -y install dnf-plugins-core epel-release \
 && dnf config-manager --set-enabled powertools \
 && dnf -y install \
      'dnf-command(config-manager)' \
      sudo wget perl which git bzip2 \
      openssh-server openssh-clients openssl-devel \
      emacs vim-enhanced jq xterm-resize \
      libXft libXpm libXext libSM mesa-libGL libnsl.x86_64 \
      glibc-locale-source glibc-langpack-en \
      stress apptainer compat-openssl10 \
      s3cmd htop \
      boost-devel diffutils ncurses-compat-libs cmake \
      glibc-static libcurl-devel \
      gfal2-util-scripts python3-gfal2 voms-clients myproxy patch \
 && dnf -y --enablerepo=epel install tini \
 # CUDA repo + packages
 && dnf config-manager --add-repo https://developer.download.nvidia.com/compute/cuda/repos/rhel8/x86_64/cuda-rhel8.repo \
 && dnf -y install cuda-toolkit-12-4 libcudnn8-8.9.7.29-1+cuda12.2 \
 # cleanup & small quality-of-life
 && dnf clean all \
 && ln -s /usr/bin/python3 /usr/bin/python \
 && rm -rf /var/cache/dnf \
 && rm -f /usr/bin/vi && ln -s /usr/bin/vim /usr/bin/vi \
 && localedef -c -f UTF-8 -i en_US en_US.UTF-8

COPY docker/purdue-af/osg/igtf-ca-certs-1.136-1.osg24.el8.noarch.rpm docker/purdue-af/osg/vo-client-138-2.osg24.el8.noarch.rpm /tmp/
RUN dnf -y --disablerepo='*' install --nogpgcheck --setopt=install_weak_deps=False \
        /tmp/igtf-ca-certs-1.136-1.osg24.el8.noarch.rpm \
        /tmp/vo-client-138-2.osg24.el8.noarch.rpm && \
     rm -f /tmp/*.rpm && dnf clean all && rm -rf /var/cache/dnf

# Install Slurm
COPY slurm/slurm-24.05.1-1.el8.x86_64.rpm /etc/
COPY slurm/slurm-configs /etc/slurm
RUN dnf localinstall -y /etc/slurm-24.05.1-1.el8.x86_64.rpm && \
    rm -rf /etc/slurm-24.05.1-1.el8.x86_64.rpm && \
    groupadd -g 1033 slurm && \
    adduser -u 2000 -g 1033 slurm && \
    chown -R slurm:slurm /etc/slurm/ && \
    mkdir -p /var/log/slurm && \
    chown -R slurm:slurm /var/log/slurm

# Configure environment
ENV SHELL=/bin/bash \
    NB_USER="${NB_USER}" \
    NB_UID=${NB_UID} \
    NB_GID=${NB_GID} \
    LANG=en_US.UTF-8 \
    LANGUAGE=en_US.UTF-8 \
    LC_ALL=en_US.UTF-8 \
    HOME="/home/${NB_USER}"

# Copy a script that we will use to correct permissions after running certain commands
COPY --chown=root:root docker/purdue-af/scripts/fix-permissions /usr/local/bin/fix-permissions
RUN chmod a+rx /usr/local/bin/fix-permissions

# Enable prompt color in the skeleton .bashrc before creating the default NB_USER
RUN sed -i 's/^#force_color_prompt=yes/force_color_prompt=yes/' /etc/skel/.bashrc

# Create NB_USER with UID=1000 and GID=100, and set permissions
RUN echo "auth requisite pam_deny.so" >> /etc/pam.d/su && \
    sed -i.bak -e 's/^%admin/#%admin/' /etc/sudoers && \
    sed -i.bak -e 's/^%sudo/#%sudo/' /etc/sudoers && \
    useradd -l -m -s /bin/bash -N -u "${NB_UID}" "${NB_USER}" && \
    chmod g+w /etc/passwd 

USER ${NB_USER}

ARG PYTHON_VERSION=3.10

# Setup work directory for backward-compatibility
RUN mkdir -p "/home/${NB_USER}/work" && \
    fix-permissions "/home/${NB_USER}"

# Install Pixi for NB_USER
WORKDIR /tmp
RUN curl -fsSL https://pixi.sh/install.sh | bash

# Set PIXI_HOME and put pixi on PATH for build
ENV PIXI_HOME="/home/${NB_USER}/.pixi"
ENV PATH="${PIXI_HOME}/bin:${PATH}"

# Copy pixi project (service env definition)
COPY --chown="${NB_USER}:${NB_GID}" docker/purdue-af/pixi.toml /srv/base-env/
WORKDIR /srv/base-env

# Install base-env into the default pixi location
RUN pixi install --environment base-env && \
    fix-permissions "/srv/base-env/.pixi" && \
    fix-permissions "${HOME}"

# Point BASE_ENV_DIR at the installed environment and use it by default
ENV BASE_ENV_DIR="/srv/base-env/.pixi/envs/base-env"
ENV PATH="${BASE_ENV_DIR}/bin:${PATH}"

# Configure container startup
ENTRYPOINT ["/usr/bin/tini", "--"]

# Copy local files as late as possible to avoid cache busting
COPY --chown=root:root docker/purdue-af/jupyter/start.sh /usr/local/bin/

# Use jupyter from base-env to generate config and do light cleanup
RUN jupyter notebook --generate-config && \
    npm cache clean --force || true && \
    jupyter lab clean && \
    rm -rf "/home/${NB_USER}/.cache/yarn" && \
    fix-permissions "${BASE_ENV_DIR}" && \
    fix-permissions "/home/${NB_USER}"

ENV JUPYTER_PORT=8888
EXPOSE $JUPYTER_PORT

# Configure container startup
CMD ["start-notebook.sh"]

# Copy local files as late as possible to avoid cache busting
COPY --chown=root:root docker/purdue-af/jupyter/start-notebook.sh docker/purdue-af/jupyter/start-singleuser.sh /usr/local/bin/
USER root
RUN echo "${NB_USER} ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/notebook && \
    echo "Defaults secure_path=\"${PATH}\"" >> /etc/sudoers.d/notebook && \
    chmod 0440 /etc/sudoers.d/notebook
RUN chmod +x /usr/local/bin/start.sh /usr/local/bin/start-notebook.sh /usr/local/bin/start-singleuser.sh

# Copy Jupyter configuration files
COPY docker/purdue-af/jupyter/jupyter_server_config.py docker/purdue-af/jupyter/docker_healthcheck.py /etc/jupyter/

# Fix permissions on /etc/jupyter as root
RUN sed -re "s/c.ServerApp/c.NotebookApp/g" \
    /etc/jupyter/jupyter_server_config.py > /etc/jupyter/jupyter_notebook_config.py && \
    fix-permissions /etc/jupyter/

# Healthcheck configuration
HEALTHCHECK --interval=5s --timeout=3s --start-period=5s --retries=3 \
 CMD /etc/jupyter/docker_healthcheck.py || exit 1

# # Set LD_LIBRARY_PATH to avoid crashes due to library mismatches
# # (Adjust for Pixi-based julia env if you wire that up)
# # ENV LD_LIBRARY_PATH="/opt/base-env/envs/julia_env/lib:$LD_LIBRARY_PATH"

# Configure XrootD, grid certificates, VOMS
RUN mkdir -p /etc/cvmfs /etc/cvmfs/SITECONF/JobConfig/ /etc/cvmfs/SITECONF/PhEDEx
COPY docker/purdue-af/xml/site-local-config.xml /etc/cvmfs/SITECONF/JobConfig/
COPY docker/purdue-af/xml/storage.xml /etc/cvmfs/SITECONF/PhEDEx/
COPY docker/purdue-af/configs/vomses /etc
RUN mkdir -p /home/${NB_USER}/.globus /home/${NB_USER}/.rnd && \
    chown -R ${NB_USER}:${NB_GID} /home/${NB_USER}/.globus /home/${NB_USER}/.rnd && \
    chmod 700 /home/${NB_USER}/.globus

ENV X509_CERT_DIR=/cvmfs/cms.cern.ch/grid/etc/grid-security/certificates

# Scripts that run before session start
COPY --chown=root:root docker/purdue-af/scripts/run-as-root.sh \
    docker/purdue-af/scripts/init-conda-kernels.sh \
    docker/purdue-af/scripts/config-extensions.sh \
    docker/purdue-af/scripts/create-symlinks.sh \
    /usr/local/bin/before-notebook.d/
RUN chmod +x /usr/local/bin/before-notebook.d/*

# Custom scripts and configurations
COPY docker/purdue-af/scripts/eos-connect.sh docker/purdue-af/jupyter/jupyter_config.json /etc/jupyter/
RUN mkdir -p /etc/cvmfs/
COPY docker/purdue-af/configs/default.local /etc/cvmfs/

# Switch back to NB_USER
USER ${NB_USER}
WORKDIR "${HOME}"