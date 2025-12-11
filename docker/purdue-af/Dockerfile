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

# ------------------------------------------------------------

# Install system dependencies
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

# Install OSG certificates and client
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
    chown -R slurm:slurm /var/log/slurm && \
    dnf clean all && rm -rf /var/cache/dnf

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

# ------------------------------------------------------------

# Create NB_USER with UID=1000 and GID=100, and set permissions
RUN sed -i 's/^#force_color_prompt=yes/force_color_prompt=yes/' /etc/skel/.bashrc && \
    echo "auth requisite pam_deny.so" >> /etc/pam.d/su && \
    sed -i.bak -e 's/^%admin/#%admin/' /etc/sudoers && \
    sed -i.bak -e 's/^%sudo/#%sudo/' /etc/sudoers && \
    useradd -l -m -s /bin/bash -N -u "${NB_UID}" "${NB_USER}" && \
    chmod g+w /etc/passwd 

# Install base environment using Pixi
ENV PIXI_HOME="/opt/pixi" \
    BASE_ENV_DIR="/opt/pixi/.pixi/envs/base-env"

WORKDIR "${PIXI_HOME}"
COPY --chown=root:root docker/purdue-af/pixi.toml "${PIXI_HOME}/"
COPY --chown=root:root docker/purdue-af/pixi-wrapper /usr/local/bin/pixi

RUN chmod +x /usr/local/bin/pixi && \
    rm -rf "${HOME}/.cache/rattler" || true && \
    PATH="${PIXI_HOME}/bin:${PATH}" \
    curl -fsSL https://pixi.sh/install.sh | bash && \
    echo "=== Before pixi install ===" && \
    ls -la "${PIXI_HOME}" && \
    pixi install --environment base-env && \
    echo "=== After pixi install ===" && \
    echo "Checking PIXI_HOME structure:" && \
    find "${PIXI_HOME}" -maxdepth 3 -type d 2>&1 | head -20 && \
    echo "Checking if BASE_ENV_DIR exists:" && \
    ls -la "${BASE_ENV_DIR}" 2>&1 || echo "BASE_ENV_DIR does not exist yet" && \
    echo "Checking .pixi directory:" && \
    ls -la "${PIXI_HOME}/.pixi" 2>&1 || echo ".pixi directory does not exist" && \
    echo "Checking .pixi/envs:" && \
    ls -la "${PIXI_HOME}/.pixi/envs" 2>&1 || echo ".pixi/envs does not exist" && \
    echo "=== Verifying Pixi environment installation ===" && \
    echo "PIXI_HOME: ${PIXI_HOME}" && \
    echo "BASE_ENV_DIR: ${BASE_ENV_DIR}" && \
    if [ -d "${BASE_ENV_DIR}" ]; then \
        ls -la "${BASE_ENV_DIR}/bin/jupyter" && \
        "${BASE_ENV_DIR}/bin/jupyter" --version && \
        echo "=== Pixi environment verified ==="; \
    else \
        echo "ERROR: BASE_ENV_DIR does not exist after pixi install"; \
        echo "Trying to find where pixi installed the environment:"; \
        find "${PIXI_HOME}" -name "jupyter" -type f 2>&1 | head -10; \
        exit 1; \
    fi && \
    rm -rf "${HOME}/.cache/rattler" || true && \
    rm -rf "${BASE_ENV_DIR}/share/jupyter/kernels/python3" || true && \
    rm -rf "${BASE_ENV_DIR}/share/jupyter/kernels/pixi-kernel-ir" || true && \
    rm -rf "${BASE_ENV_DIR}/share/jupyter/kernels/pixi-kernel-ark" || true


# ------------------------------------------------------------


# Configure XrootD, grid certificates, VOMS
RUN mkdir -p /etc/cvmfs /etc/cvmfs/SITECONF/JobConfig/ /etc/cvmfs/SITECONF/PhEDEx
COPY docker/purdue-af/xml/site-local-config.xml /etc/cvmfs/SITECONF/JobConfig/
COPY docker/purdue-af/xml/storage.xml /etc/cvmfs/SITECONF/PhEDEx/
COPY docker/purdue-af/configs/vomses /etc
COPY docker/purdue-af/configs/default.local /etc/cvmfs/
RUN mkdir -p "${HOME}/.globus" "${HOME}/.rnd" && \
    chown -R ${NB_USER}:${NB_GID} "${HOME}/.globus" "${HOME}/.rnd" && \
    chmod 700 "${HOME}/.globus"
ENV X509_CERT_DIR=/cvmfs/cms.cern.ch/grid/etc/grid-security/certificates


# ------------------------------------------------------------

# Jupyter scripts

COPY docker/purdue-af/jupyter/jupyter_server_config.py \
    docker/purdue-af/jupyter/docker_healthcheck.py \
    docker/purdue-af/scripts/eos-connect.sh \
    docker/purdue-af/jupyter/jupyter_config.json \
    /etc/jupyter/
COPY --chown=root:root docker/purdue-af/scripts/run-as-root.sh \
    docker/purdue-af/scripts/config-extensions.sh \
    docker/purdue-af/scripts/create-symlinks.sh \
    /usr/local/bin/before-notebook.d/
# docker/purdue-af/scripts/init-kernels.sh \
COPY --chown=root:root \
    docker/purdue-af/jupyter/start.sh \
    docker/purdue-af/jupyter/start-notebook.sh \
    docker/purdue-af/jupyter/start-singleuser.sh \
    /usr/local/bin/

RUN chmod +x /usr/local/bin/* && \
    chmod +x /usr/local/bin/before-notebook.d/* && \
    sed -re "s/c.ServerApp/c.NotebookApp/g" \
        /etc/jupyter/jupyter_server_config.py > /etc/jupyter/jupyter_notebook_config.py && \
    fix-permissions /etc/jupyter/ && fix-permissions "${HOME}"

# Set PATH before switching user so it's available to all users
ENV PATH="/usr/local/bin:${BASE_ENV_DIR}/bin:${PIXI_HOME}/bin:${PATH}"

# ------------------------------------------------------------

# Container setup as NB_USER

USER ${NB_USER}

RUN echo "=== Running as ${NB_USER} ===" && \
    echo "PATH: ${PATH}" && \
    echo "BASE_ENV_DIR: ${BASE_ENV_DIR}" && \
    echo "PIXI_HOME: ${PIXI_HOME}" && \
    echo "=== Checking if BASE_ENV_DIR exists ===" && \
    ls -la "${BASE_ENV_DIR}" || echo "ERROR: BASE_ENV_DIR does not exist" && \
    echo "=== Checking if bin directory exists ===" && \
    ls -la "${BASE_ENV_DIR}/bin" 2>&1 || echo "ERROR: bin directory does not exist" && \
    echo "=== Checking for jupyter binary ===" && \
    ls -la "${BASE_ENV_DIR}/bin/jupyter" 2>&1 || echo "ERROR: jupyter binary not found" && \
    echo "=== Checking if PIXI_HOME exists ===" && \
    ls -la "${PIXI_HOME}" 2>&1 || echo "ERROR: PIXI_HOME does not exist" && \
    echo "=== Checking pixi binary ===" && \
    which pixi || echo "ERROR: pixi not in PATH" && \
    echo "=== Attempting to use jupyter ===" && \
    /opt/pixi/.pixi/envs/base-env/bin/jupyter notebook --generate-config && \
    npm cache clean --force || true && \
    /opt/pixi/.pixi/envs/base-env/bin/jupyter lab clean && \
    rm -rf "${HOME}/.cache/yarn"

WORKDIR "${HOME}"
EXPOSE 8888
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["start-notebook.sh"]
HEALTHCHECK --interval=5s --timeout=3s --start-period=5s --retries=3 \
    CMD /etc/jupyter/docker_healthcheck.py || exit 1

