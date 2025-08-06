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

# Install OS dependencies
RUN dnf -y update && \
    dnf -y install epel-release dnf-plugins-core && \
    dnf config-manager --set-enabled powertools && \
    # Install OSG repository
    dnf -y install https://repo.opensciencegrid.org/osg/3.6/osg-3.6-el8-release-latest.rpm && \
    # Install other repositories
    dnf -y install https://dl.fedoraproject.org/pub/epel/8/Everything/x86_64/Packages/t/tini-0.19.0-1.el8.x86_64.rpm && \
    # Install packages
    dnf -y install \
        'dnf-command(config-manager)' \
        sudo wget perl which git bzip2 \
        openssh-server openssh-clients openssl-devel \
        emacs vim-enhanced jq xterm-resize \
        libXft libXpm libXext libSM mesa-libGL libnsl.x86_64 \
        glibc-locale-source glibc-langpack-en \
        stress apptainer compat-openssl10 \
        s3cmd htop \
        boost-devel diffutils ncurses-compat-libs cmake \
        glibc-static \
        python3-gfal2 && \
    # Add CUDA repository
    dnf config-manager --add-repo https://developer.download.nvidia.com/compute/cuda/repos/rhel8/x86_64/cuda-rhel8.repo && \
    dnf -y install cuda-toolkit-12-4 libcudnn8-8.9.7.29-1+cuda12.2 libcurl-devel && \
    dnf clean all && \
    rm -rf /var/cache/dnf && \
    rm /usr/bin/vi && \
    ln -s /usr/bin/vim /usr/bin/vi && \
    localedef -c -f UTF-8 -i en_US en_US.UTF-8
# cudnn9-cuda-12-4-9.1.1.17-1.x86_64.rpm
# libcudnn9-cuda-12-9.7.1.26-1.x86_64.rpm
RUN yum install -y vo-client voms-clients vo-client myproxy patch && yum clean all && rm -rf /var/cache/yum

# Configure environment
ENV CONDA_DIR=/opt/conda \
    SHELL=/bin/bash \
    NB_USER="${NB_USER}" \
    NB_UID=${NB_UID} \
    NB_GID=${NB_GID} \
    LANG=en_US.UTF-8 \
    LANGUAGE=en_US.UTF-8 \
    LC_ALL=en_US.UTF-8 \
    PATH="/opt/conda/bin:${PATH}" \
    HOME="/home/${NB_USER}"

# Copy a script that we will use to correct permissions after running certain commands
COPY --chown=root:root docker/cmsaf-alma8/scripts/fix-permissions /usr/local/bin/fix-permissions
RUN chmod a+rx /usr/local/bin/fix-permissions

# Enable prompt color in the skeleton .bashrc before creating the default NB_USER
RUN sed -i 's/^#force_color_prompt=yes/force_color_prompt=yes/' /etc/skel/.bashrc && \
    echo 'eval "$(command conda shell.bash hook 2> /dev/null)"' >> /etc/skel/.bashrc

# Create NB_USER with UID=1000 and GID=100, and set permissions
RUN echo "auth requisite pam_deny.so" >> /etc/pam.d/su && \
    sed -i.bak -e 's/^%admin/#%admin/' /etc/sudoers && \
    sed -i.bak -e 's/^%sudo/#%sudo/' /etc/sudoers && \
    useradd -l -m -s /bin/bash -N -u "${NB_UID}" "${NB_USER}" && \
    mkdir -p "${CONDA_DIR}" && \
    chown "${NB_USER}:${NB_GID}" "${CONDA_DIR}" && \
    chmod g+w /etc/passwd && \
    fix-permissions "${HOME}" && \
    fix-permissions "${CONDA_DIR}"

USER ${NB_USER}

# Pin python version here, or set it to "default"
ARG PYTHON_VERSION=3.10

# Setup work directory for backward-compatibility
RUN mkdir -p "/home/${NB_USER}/work" && \
    fix-permissions "/home/${NB_USER}"

# Download and install Micromamba, and initialize Conda prefix
COPY --chown="${NB_USER}:${NB_GID}" docker/cmsaf-alma8/scripts/initial-condarc "${CONDA_DIR}/.condarc"
WORKDIR /tmp
COPY docker/cmsaf-alma8/src/micromamba.tar.bz2 /tmp/
# wget -qO /tmp/micromamba.tar.bz2 "https://micromamba.snakepit.net/api/micromamba/linux-64/latest" && \

RUN set -x  && \
    tar -xvjf /tmp/micromamba.tar.bz2 --strip-components=1 bin/micromamba && \
    PYTHON_SPECIFIER="python=${PYTHON_VERSION}" && \
    if [[ "${PYTHON_VERSION}" == "default" ]]; then PYTHON_SPECIFIER="python"; fi && \
    ./micromamba install \
        --root-prefix="${CONDA_DIR}" \
        --prefix="${CONDA_DIR}" \
        --yes \
        "${PYTHON_SPECIFIER}" \
        'mamba' \
        'jupyter_core' && \
    rm micromamba && \
    mamba clean --all -f -y && \
    fix-permissions "${CONDA_DIR}" && \
    fix-permissions "/home/${NB_USER}"
# mamba list python | grep '^python ' | tr -s ' ' | cut -d ' ' -f 1,2 >> "${CONDA_DIR}/conda-meta/pinned" && \


# Configure container startup
ENTRYPOINT ["/usr/bin/tini", "--"]

# Copy local files as late as possible to avoid cache busting
COPY docker/cmsaf-alma8/jupyter/start.sh /usr/local/bin/

WORKDIR /tmp
RUN mamba install --quiet --yes \
        notebook jupyterhub jupyterlab=4.4.3 nodejs==18.11.0 \
        nb_conda_kernels metakernel rucio-clients pytest lz4 ipywidgets ipympl bokeh==2.4.2 \
        julia &&\
    jupyter notebook --generate-config && \
    mamba clean --all -f -y && \
    npm cache clean --force && \
    jupyter lab clean && \
    rm -rf "/home/${NB_USER}/.cache/yarn" && \
    fix-permissions "${CONDA_DIR}" && \
    fix-permissions "/home/${NB_USER}"

ENV JUPYTER_PORT=8888
EXPOSE $JUPYTER_PORT

# Configure container startup
CMD ["start-notebook.sh"]

# Copy local files as late as possible to avoid cache busting
COPY docker/cmsaf-alma8/jupyter/start-notebook.sh docker/cmsaf-alma8/jupyter/start-singleuser.sh /usr/local/bin/

# Copy Jupyter configuration files
COPY docker/cmsaf-alma8/jupyter/jupyter_server_config.py docker/cmsaf-alma8/jupyter/docker_healthcheck.py /etc/jupyter/

# Fix permissions on /etc/jupyter as root
USER root
RUN sed -re "s/c.ServerApp/c.NotebookApp/g" \
    /etc/jupyter/jupyter_server_config.py > /etc/jupyter/jupyter_notebook_config.py && \
    fix-permissions /etc/jupyter/

# Healthcheck configuration
HEALTHCHECK --interval=5s --timeout=3s --start-period=5s --retries=3 \
CMD /etc/jupyter/docker_healthcheck.py || exit 1

# # Set LD_LIBRARY_PATH to avoid crashes due to library mismatches
# ENV LD_LIBRARY_PATH="/opt/conda/envs/julia_env/lib:$LD_LIBRARY_PATH"

# # Install the Julia kernel via IJulia
# RUN julia -e 'using Pkg; Pkg.add("IJulia"); using IJulia; IJulia.installkernel("Julia", "--depwarn=no")'

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

# Configure XrootD, grid certificates, VOMS
RUN mkdir -p /etc/cvmfs /etc/cvmfs/SITECONF/JobConfig/ /etc/cvmfs/SITECONF/PhEDEx
COPY docker/cmsaf-alma8/xml/site-local-config.xml /etc/cvmfs/SITECONF/JobConfig/
COPY docker/cmsaf-alma8/xml/storage.xml /etc/cvmfs/SITECONF/PhEDEx/
COPY docker/cmsaf-alma8/configs/vomses /etc
RUN mkdir -p /home/${NB_USER}/.globus /home/${NB_USER}/.rnd && \
    chown -R ${NB_USER}:${NB_GID} /home/${NB_USER}/.globus /home/${NB_USER}/.rnd && \
    chmod 700 /home/${NB_USER}/.globus

ENV X509_CERT_DIR=/cvmfs/cms.cern.ch/grid/etc/grid-security/certificates

RUN pip install --no-cache-dir --upgrade \
    jupyterlab-git==0.51.2 \
    jupyter-resource-usage==1.1.1 \
    jupyterlab_iframe==0.5.0 \
    jupyter-archive==3.4.0 \
    jupyterlab_favorites==3.2.2 \
    purdue-af-shutdown-button==0.1.0 \
    purdue-af-grafana-iframe==0.1.0 \
    jupyterlab-topbar-text==1.1.0 \
    jupyterlab-theme-toggler==1.1.0 \
    dask-gateway==2025.4.0 \
    git+https://github.com/PurdueAF/jupyterlab-prometheus-alerts.git \
    jupyter-ai[all]==2.31.6

# Find and build the lab extension
RUN find /opt/conda -name "jupyterlab_prometheus_alerts*" -type d | head -1 | xargs -I {} sh -c 'cd "{}" && npm install && npm run build:all && jupyter labextension install .'

# Enable the server extension
RUN jupyter server extension enable prometheus_alerts

# Verify installation (with error handling)
RUN jupyter server extension list | grep prometheus_alerts || echo "Server extension not found" && \
    jupyter labextension list | grep jupyterlab-prometheus-alerts || echo "Lab extension not found"

COPY docker/cmsaf-alma8/genaistudio /opt/conda/lib/python3.12/site-packages/genaistudio
RUN pip install --no-cache-dir -e /opt/conda/lib/python3.12/site-packages/genaistudio

# Install Dask labextension
RUN git clone https://github.com/kondratyevd/dask-labextension.git
WORKDIR /tmp/dask-labextension
RUN pip install --no-cache-dir .
WORKDIR /tmp
RUN rm -rf dask-labextension

# Configure JupyterLab extensions
RUN jupyter labextension disable "@jupyterlab/apputils-extension:announcements"
# RUN jupyter nbextension enable --py --sys-prefix ipympl

# Scripts that run before session start
COPY --chown=root:root docker/cmsaf-alma8/scripts/run-as-root.sh \
    docker/cmsaf-alma8/scripts/init-conda-kernels.sh \
    docker/cmsaf-alma8/scripts/config-extensions.sh \
    docker/cmsaf-alma8/scripts/create-symlinks.sh \
    /usr/local/bin/before-notebook.d/
RUN chmod +x /usr/local/bin/before-notebook.d/*

# Custom scripts and configurations
COPY docker/cmsaf-alma8/scripts/eos-connect.sh docker/cmsaf-alma8/jupyter/jupyter_config.json /etc/jupyter/
RUN mkdir -p /etc/cvmfs/
COPY docker/cmsaf-alma8/configs/default.local /etc/cvmfs/

# Switch back to NB_USER
USER ${NB_USER}

WORKDIR "${HOME}"