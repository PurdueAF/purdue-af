# Copyright (c) Jupyter Development Team.
# Distributed under the terms of the Modified BSD License.
ARG OWNER=jupyter
ARG ROOT_CONTAINER=gitlab-registry.cern.ch/linuxsupport/alma8-base:latest
FROM $ROOT_CONTAINER

LABEL maintainer="Dmitry Kondratyev <dkondra@purdue.edu>"
ARG NB_USER="jovyan"
ARG NB_UID="1000"
ARG NB_GID="100"

# Fix: https://github.com/hadolint/hadolint/wiki/DL4006
# Fix: https://github.com/koalaman/shellcheck/wiki/SC3014
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

USER root

# Install OS dependencies
RUN yum update -y  && \
    yum upgrade -y && \
    yum install -y \
        https://dl.fedoraproject.org/pub/epel/epel-release-latest-8.noarch.rpm \
        https://repo.opensciencegrid.org/osg/3.6/osg-3.6-el8-release-latest.rpm \
        https://dl.fedoraproject.org/pub/epel/8/Everything/x86_64/Packages/t/tini-0.19.0-1.el8.x86_64.rpm \
        sudo wget perl which nodejs git bzip2\
        openssh-server openssh-clients openssl-devel \
        emacs vim-enhanced jq \
        libXft libXpm libXext libSM mesa-libGL libnsl.x86_64 \
        glibc-locale-source glibc-langpack-en && \
    yum install -y \
        voms-clients vo-client myproxy gfal2-util gfal2-all stress && \
    yum --enablerepo epel-testing install -y \
        s3cmd htop

RUN dnf -y install boost-devel diffutils ncurses-compat-libs && \
    dnf -y --enablerepo=powertools install glibc-static

RUN yum clean all && \
    rm -rf /var/lib/apt/lists/* && \
    rm /usr/bin/vi && \
    ln -s /usr/bin/vim /usr/bin/vi && \
    localedef -c -f UTF-8 -i en_US en_US.UTF-8 && \
    export LC_ALL=en_US.UTF-8

# Configure environment
ENV CONDA_DIR=/opt/conda \
    SHELL=/bin/bash \
    NB_USER="${NB_USER}" \
    NB_UID=${NB_UID} \
    NB_GID=${NB_GID} \
    LC_ALL=en_US.UTF-8 \
    LANG=en_US.UTF-8 \
    LANGUAGE=en_US.UTF-8
ENV PATH="${CONDA_DIR}/bin:${PATH}" \
    HOME="/home/${NB_USER}"

# Copy a script that we will use to correct permissions after running certain commands
COPY fix-permissions /usr/local/bin/fix-permissions
RUN chmod a+rx /usr/local/bin/fix-permissions

# Enable prompt color in the skeleton .bashrc before creating the default NB_USER
# hadolint ignore=SC2016
RUN sed -i 's/^#force_color_prompt=yes/force_color_prompt=yes/' /etc/skel/.bashrc && \
   # Add call to conda init script see https://stackoverflow.com/a/58081608/4413446
   echo 'eval "$(command conda shell.bash hook 2> /dev/null)"' >> /etc/skel/.bashrc

# Create NB_USER with name jovyan user with UID=1000 and in the 'users' group
# and make sure these dirs are writable by the `users` group.
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

# Download and install Micromamba, and initialize Conda prefix.
#   <https://github.com/mamba-org/mamba#micromamba>
#   Similar projects using Micromamba:
#     - Micromamba-Docker: <https://github.com/mamba-org/micromamba-docker>
#     - repo2docker: <https://github.com/jupyterhub/repo2docker>
# Install Python, Mamba and jupyter_core
# Cleanup temporary files and remove Micromamba
# Correct permissions
# Do all this in a single RUN command to avoid duplicating all of the
# files across image layers when the permissions change
COPY --chown="${NB_USER}:${NB_GID}" initial-condarc "${CONDA_DIR}/.condarc"
WORKDIR /tmp
RUN set -x && \
    wget -qO /tmp/micromamba.tar.bz2 "https://micromamba.snakepit.net/api/micromamba/linux-64/latest" && \
    tar -xvjf /tmp/micromamba.tar.bz2 --strip-components=1 bin/micromamba && \
    rm /tmp/micromamba.tar.bz2 && \
    PYTHON_SPECIFIER="python=${PYTHON_VERSION}" && \
    if [[ "${PYTHON_VERSION}" == "default" ]]; then PYTHON_SPECIFIER="python"; fi && \
    # Install the packages
    ./micromamba install \
        --root-prefix="${CONDA_DIR}" \
        --prefix="${CONDA_DIR}" \
        --yes \
        "${PYTHON_SPECIFIER}" \
        'mamba' \
        'jupyter_core' && \
    rm micromamba && \
    # Pin major.minor version of python
    mamba list python | grep '^python ' | tr -s ' ' | cut -d ' ' -f 1,2 >> "${CONDA_DIR}/conda-meta/pinned" && \
    mamba clean --all -f -y && \
    fix-permissions "${CONDA_DIR}" && \
    fix-permissions "/home/${NB_USER}"

# Configure container startup
ENTRYPOINT ["/usr/bin/tini", "--"]

# Copy local files as late as possible to avoid cache busting
COPY jupyter/start.sh /usr/local/bin/

WORKDIR /tmp
RUN mamba install --quiet --yes \
    notebook jupyterhub jupyterlab=4.1.0 \
    nb_conda_kernels rucio-clients pytest lz4 ipywidgets bokeh==2.4.2 \
    root && \
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
COPY jupyter/start-notebook.sh jupyter/start-singleuser.sh /usr/local/bin/
# Currently need to have both jupyter_notebook_config and jupyter_server_config to support classic and lab
COPY jupyter/jupyter_server_config.py jupyter/docker_healthcheck.py /etc/jupyter/

# Fix permissions on /etc/jupyter as root
USER root

# Legacy for Jupyter Notebook Server, see: [#1205](https://github.com/jupyter/docker-stacks/issues/1205)
RUN sed -re "s/c.ServerApp/c.NotebookApp/g" \
    /etc/jupyter/jupyter_server_config.py > /etc/jupyter/jupyter_notebook_config.py && \
    fix-permissions /etc/jupyter/

# HEALTHCHECK documentation: https://docs.docker.com/engine/reference/builder/#healthcheck
# This healtcheck works well for `lab`, `notebook`, `nbclassic`, `server` and `retro` jupyter commands
# https://github.com/jupyter/docker-stacks/issues/915#issuecomment-1068528799
HEALTHCHECK --interval=5s --timeout=3s --start-period=5s --retries=3 \
    CMD /etc/jupyter/docker_healthcheck.py || exit 1

#RUN chown root:$USER ~/.ssh/config &&\
#    chmod 644 ~/.ssh/config

# Set alternatives
RUN alternatives --set python /usr/bin/python3

# Install Slurm
COPY rpms/slurm/slurm-21.08.8-2.el8.x86_64.rpm /etc/
COPY slurm /etc/slurm
RUN yum localinstall -y /etc/slurm-21.08.8-2.el8.x86_64.rpm &&\
    rm -rf /etc/slurm-21.08.8-2.el8.x86_64.rpm &&\
    groupadd -g 1033 slurm &&\
    adduser -u 2000 -g 1033 slurm &&\
    chown -R slurm:slurm /etc/slurm/ &&\
    mkdir -p /var/log/slurm &&\
    chown -R slurm:slurm /var/log/slurm

# Configure XrootD, grid certificates, VOMS
RUN mkdir -p /etc/cvmfs /etc/cvmfs/SITECONF /etc/cvmfs/SITECONF/JobConfig/ /etc/cvmfs/SITECONF/PhEDEx
COPY xml/site-local-config.xml /etc/cvmfs/SITECONF/JobConfig/
COPY xml/storage.xml /etc/cvmfs/SITECONF/PhEDEx/
# May also need this to avoid errors:
# cp /cvmfs/cms.cern.ch/SITECONF/T2_US_Purdue/storage.json /etc/cvmfs/
ADD vomses /etc
RUN mkdir -p /home/jovyan/.globus /home/jovyan/.rnd && \
    chmod -s /home/jovyan/.globus
ENV X509_CERT_DIR=/cvmfs/cms.cern.ch/grid/etc/grid-security/certificates

# Install Dask packages
# RUN pip install distributed==2023.3.2 dask==2023.3.2 \
#     dask_gateway==2023.9.0 dask_jobqueue==0.8.1 dask[dataframe]

# Install JupyterLab extensions
RUN pip install --upgrade \
    jupyterlab-git \
    # dask-labextension \
    jupyter-resource-usage \
    jupyterlab_iframe \
    "git+https://github.com/jupyterlab-contrib/jupyterlab-topbar.git#egg=jupyterlab-topbar-text&subdirectory=packages/topbar-text-extension"   \
    "git+https://github.com/jupyterlab-contrib/jupyterlab-topbar.git#egg=jupyterlab-theme-toggler&subdirectory=packages/theme-toggler-extension" \
    "git+https://github.com/PurdueAF/purdue-af-extensions.git#egg=jupyterlab-shutdown&subdirectory=jupyterlab-shutdown" \
    "git+https://github.com/PurdueAF/purdue-af-extensions.git#egg=purdue-af-grafana-iframe&subdirectory=purdue-af-grafana-iframe"

# Install custom extensions
# ENV EXTENSIONS_DIR "/etc/jupyter/extensions"
# RUN git clone https://github.com/kondratyevd/toolbar-button \
#     && cd toolbar-button \
#     && mkdir -p ${EXTENSIONS_DIR}/toolbar-button \
#     && cp button_script.sh conda_env_export.py ${EXTENSIONS_DIR}/toolbar-button \
#     && pip install .   \
#     && cd ..  \
#     && rm -rf toolbar-button
# Build JupyterLab
# RUN jupyter lab build --dev-build=False

RUN jupyter labextension disable "@jupyterlab/apputils-extension:announcements"

# Scripts that run before session start
COPY scripts/run-as-root.sh \
  scripts/init-conda-kernels.sh \
  scripts/config-extensions.sh \
  scripts/create-symlinks.sh \
  /usr/local/bin/before-notebook.d/

# Bash profile scripts - not working anymore
# COPY scripts/aliases.sh scripts/init-conda.sh /etc/profile.d/

# Custom scripts
COPY scripts/eos-connect.sh jupyter/jupyter_config.json /etc/jupyter/

# Switch back from root user
USER USER ${NB_USER}
WORKDIR "${HOME}"
