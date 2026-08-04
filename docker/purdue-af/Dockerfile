# Copyright (c) Jupyter Development Team.
# Distributed under the terms of the Modified BSD License.
# Multi-stage build: system -> stack -> runtime
#
# EXPERIMENTAL slim rebuild of docker/purdue-af (target: purdue-af:0.13.0-preX).
# Differences vs docker/purdue-af/Dockerfile — see README.md for the numbers:
#   - base: nvidia/cuda devel on Rocky Linux 8 (EL8, Alma8-compatible) instead
#     of CERN alma8-base + a hand-installed cuda-toolkit-12-4. CUDA arrives as
#     immutable NVIDIA-maintained base layers; drops nsight-systems and the
#     ~1.2 GB of kernel firmware that alma8-base + `dnf upgrade` carried.
#   - libcudnn8 stays pinned (the base image ships NVIDIA's cuda.repo).
#   - voms-clients-cpp instead of voms-clients (drops the Java 8 dependency).
#   - CERN krb5/CA defaults vendored in configs/ (extracted byte-identical
#     from alma8-base:20250501-1), since the Rocky base has none.
# Everything else (OSG, Slurm, user setup, pixi stack, runtime) matches the
# original stage for stage.

# ------------------------------------------------------------
# Stage: system (dnf, OSG, Slurm, user; no pixi)
# ------------------------------------------------------------
FROM geddes-registry.rcac.purdue.edu/docker-hub-cache/nvidia/cuda:12.4.1-devel-rockylinux8 AS system

LABEL maintainer="Dmitry Kondratyev <dkondra@purdue.edu>"
ARG NB_USER="jovyan"
ARG NB_UID="1000"
ARG NB_GID="100"

# Fix: https://github.com/hadolint/hadolint/wiki/DL4006
# Fix: https://github.com/koalaman/shellcheck/wiki/SC3014
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# JupyterHub singleuser image: stays root so the hub/KubeSpawner can set the
# runtime UID; the spawned container does not run as root.
# hadolint ignore=DL3002
USER root

# Install system dependencies.
# --disablerepo=cuda everywhere except the cuDNN line: the base image's
# cuda.repo is NVIDIA's ROLLING rhel8 repo (contains CUDA 13.x, whose cccl
# obsoletes cuda-cccl-12-4), so an unconstrained upgrade would cross-grade
# CUDA 12.4 → 13 and fail. CUDA must stay exactly at the base image's pins.
# --exclude=linux-firmware*: kernel firmware is useless in a container and
# costs >1 GB if an upgrade ever drags it in.
# cuda-toolkit/nsight are NOT installed here — the base image provides CUDA;
# only cuDNN is added on top, pinned, from the base's cuda repo.
RUN set -euo pipefail \
 && dnf -y upgrade --disablerepo=cuda --exclude='linux-firmware*' \
 && dnf -y install --disablerepo=cuda dnf-plugins-core epel-release \
 && dnf config-manager --set-enabled powertools \
 && dnf -y install --disablerepo=cuda \
      'dnf-command(config-manager)' \
      sudo wget perl which git bzip2 bzip2-devel zlib-devel \
      openssh-server openssh-clients openssl-devel \
      emacs vim-enhanced jq xterm-resize \
      libXft libXpm libXext libSM mesa-libGL libnsl.x86_64 \
      glibc-locale-source glibc-langpack-en \
      stress apptainer compat-openssl10 \
      s3cmd htop \
      boost-devel diffutils ncurses-compat-libs cmake \
      glibc-static libcurl-devel \
      gfal2-util-scripts python3-gfal2 voms-clients-cpp myproxy patch \
      # base-parity: alma8-base preinstalled these; the Rocky+CUDA base does
      # not (found by diffing `rpm -qa` of the two bases after "ps: command
      # not found" in a 0.13.0-pre1 session; krb5-workstation provides the
      # kinit/klist that eos-connect.sh needs; libatomic so `gcc -latomic`
      # works with the system toolchain)
      procps-ng psmisc krb5-workstation xz cpio libatomic \
 && dnf -y --disablerepo=cuda --enablerepo=epel install tini \
 # cuDNN pin (same version the 0.12.x image shipped); the only line that
 # uses the cuda repo — an exact-version install cannot pull obsoletes
 && dnf -y install libcudnn8-8.9.7.29-1+cuda12.2 \
 # cleanup & small quality-of-life
 && dnf clean all \
 && ln -s /usr/bin/python3 /usr/bin/python \
 && rm -rf /var/cache/dnf \
 && rm -f /usr/bin/vi && ln -s /usr/bin/vim /usr/bin/vi \
 && localedef -c -f UTF-8 -i en_US en_US.UTF-8

# CERN kerberos + CA defaults (alma8-base shipped these; Rocky does not).
# eos-connect.sh runs `kinit <user>@CERN.CH` and expects the CERN.CH realm.
COPY docker/purdue-af/configs/krb5.conf.d/ /etc/krb5.conf.d/
COPY docker/purdue-af/configs/certs/ /etc/pki/tls/certs/

# Install OSG certificates and client
COPY docker/purdue-af/osg/igtf-ca-certs-1.136-1.osg24.el8.noarch.rpm docker/purdue-af/osg/vo-client-138-2.osg24.el8.noarch.rpm /tmp/
RUN dnf -y --disablerepo='*' install --nogpgcheck --setopt=install_weak_deps=False \
        /tmp/igtf-ca-certs-1.136-1.osg24.el8.noarch.rpm \
        /tmp/vo-client-138-2.osg24.el8.noarch.rpm && \
     rm -f /tmp/*.rpm && dnf clean all && rm -rf /var/cache/dnf

# Install Slurm
COPY slurm/slurm-25.11.4-1.el8.x86_64.rpm /etc/
COPY slurm/slurm-configs-hammer /etc/slurm
RUN set -euo pipefail \
 && dnf localinstall -y /etc/slurm-25.11.4-1.el8.x86_64.rpm \
 && rm -rf /etc/slurm-25.11.4-1.el8.x86_64.rpm \
 && groupadd -g 1033 slurm || true \
 && adduser -u 2000 -g 1033 slurm || true \
 && chown -R slurm:slurm /etc/slurm/ \
 && mkdir -p /var/log/slurm \
 && chown -R slurm:slurm /var/log/slurm \
 && dnf clean all && rm -rf /var/cache/dnf

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

# Create NB_USER with UID=1000 and GID=100, and set permissions
RUN sed -i 's/^#force_color_prompt=yes/force_color_prompt=yes/' /etc/skel/.bashrc && \
    echo "auth requisite pam_deny.so" >> /etc/pam.d/su && \
    sed -i.bak -e 's/^%admin/#%admin/' /etc/sudoers && \
    sed -i.bak -e 's/^%sudo/#%sudo/' /etc/sudoers && \
    useradd -l -m -s /bin/bash -N -u "${NB_UID}" "${NB_USER}" && \
    chmod g+w /etc/passwd

# ------------------------------------------------------------
# Stage: stack (all pixi + extensions + config generation)
# ------------------------------------------------------------
FROM system AS stack

ARG PIXI_VERSION="0.62.2" # keep in sync with ci-pixi-*.yml (pixi-version) + renovate.json5 (constraints.pixi)

ENV PIXI_HOME="/opt/pixi" \
    BASE_ENV_DIR="/opt/pixi/.pixi/envs/base-env"

WORKDIR "${PIXI_HOME}"
# pixi.lock makes the env reproducible: `pixi install --locked` below fails
# if manifest and lock disagree instead of silently re-solving to latest.
COPY --chown=root:root pixi/base/pixi.toml pixi/base/pixi.lock "${PIXI_HOME}/"
COPY --chown=root:root docker/purdue-af/pixi-wrapper /usr/local/bin/pixi

RUN chmod +x /usr/local/bin/pixi && \
    rm -rf "${HOME}/.cache/rattler" || true && \
    mkdir -p "${PIXI_HOME}/bin" && \
    curl -fsSL --retry 3 --retry-delay 2 \
        -o /tmp/pixi.tar.gz \
        "https://github.com/prefix-dev/pixi/releases/download/v${PIXI_VERSION}/pixi-x86_64-unknown-linux-musl.tar.gz" && \
    tar -xzf /tmp/pixi.tar.gz -C "${PIXI_HOME}/bin" pixi && \
    chmod +x "${PIXI_HOME}/bin/pixi" && \
    rm -f /tmp/pixi.tar.gz && \
    PATH="${PIXI_HOME}/bin:${PATH}" \
    "${PIXI_HOME}/bin/pixi" --version && \
    PATH="${PIXI_HOME}/bin:${PATH}" \
    pixi install --locked --environment base-env && \
    [ -d "${BASE_ENV_DIR}" ] || (echo "ERROR: BASE_ENV_DIR does not exist after pixi install" && exit 1) && \
    rm -rf "${HOME}/.cache/rattler" || true && \
    rm -rf "${BASE_ENV_DIR}/share/jupyter/kernels/pixi-kernel-ir" || true && \
    rm -rf "${BASE_ENV_DIR}/share/jupyter/kernels/pixi-kernel-ark" || true && \
    cp "${BASE_ENV_DIR}/lib/python3.12/site-packages/jupyter_vscode_proxy/icons/vscode.svg" \
       "${BASE_ENV_DIR}/lib/python3.12/site-packages/jupyter_vscode_proxy/icons/code-server.svg"

# Coding-agent CLIs. Installed with the base env's node (nodejs 22 from
# pixi/base) into their own prefix so a pixi rebuild cannot clobber them, and
# so the runtime stage can COPY them as one directory. Both ship the same
# config files their code-server extensions read, so registering the AF MCP
# server once (config-agents.sh) covers terminal and editor alike.
ARG CLAUDE_CODE_VERSION="2.1.221"
ARG CODEX_VERSION="0.146.0"
ENV NPM_GLOBAL="/opt/npm-global"
# hadolint ignore=DL3016  # versions ARE pinned, via the ARGs above
RUN PATH="${BASE_ENV_DIR}/bin:${PATH}" \
    npm install -g --prefix "${NPM_GLOBAL}" --no-fund --no-audit \
        "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
        "@openai/codex@${CODEX_VERSION}" && \
    PATH="${NPM_GLOBAL}/bin:${BASE_ENV_DIR}/bin:${PATH}" claude --version && \
    PATH="${NPM_GLOBAL}/bin:${BASE_ENV_DIR}/bin:${PATH}" codex --version && \
    rm -rf "${HOME}/.npm" || true

# Import-smoke every direct dependency of the base env, each in a fresh
# interpreter — catches broken installs (e.g. libstdc++ shadowing à la
# GLIBCXX_3.4.29) at build time instead of in a user session.
COPY --chown=root:root pixi/check-env.py /tmp/check-env.py
RUN "${BASE_ENV_DIR}/bin/python" /tmp/check-env.py \
        --manifest "${PIXI_HOME}/pixi.toml" --env base-env && \
    rm -f /tmp/check-env.py

# ------------------------------------------------------------
# Stage: runtime (lightweight; COPY only, no builds or installation routines)
# ------------------------------------------------------------
FROM system AS runtime

# Re-declare for COPY path (ARGs not inherited; default matches system stage)
ARG NB_USER="jovyan"

# NB: the NVIDIA base bakes NVIDIA_VISIBLE_DEVICES=all and this cluster's
# container runtime honors it for unprivileged pods — a 0-GPU pod running
# this image raw would see EVERY GPU on its node (verified on paf-a01:
# both T4s exposed). The image deliberately does NOT override it: baking
# `void` here could only be undone by device-plugin env injection, and GPU
# sessions must never depend on that. Instead the hub hides GPUs from
# 0-GPU sessions at spawn time (gpu-availability.py modify_pod_hook
# injects NVIDIA_VISIBLE_DEVICES=void into the pod spec); GPU sessions
# keep the exact same env semantics as the 0.12.x image.
ENV PIXI_HOME="/opt/pixi" \
    BASE_ENV_DIR="/opt/pixi/.pixi/envs/base-env" \
    X509_CERT_DIR=/cvmfs/cms.cern.ch/grid/etc/grid-security/certificates

# Copy pixi env from stack stage
COPY --from=stack /opt/pixi /opt/pixi

# Coding-agent CLIs (claude, codex) — their shebangs resolve `node` from PATH,
# which the ENV below puts in the base env.
COPY --from=stack /opt/npm-global /opt/npm-global

# Pixi wrapper in final image (same path as before)
COPY --chown=root:root docker/purdue-af/pixi-wrapper /usr/local/bin/pixi

# Configure XrootD, grid certificates, VOMS
RUN mkdir -p /etc/cvmfs /etc/cvmfs/SITECONF/JobConfig/ /etc/cvmfs/SITECONF/PhEDEx
COPY docker/purdue-af/xml/site-local-config.xml /etc/cvmfs/SITECONF/JobConfig/
COPY docker/purdue-af/xml/storage.xml /etc/cvmfs/SITECONF/PhEDEx/
COPY docker/purdue-af/configs/vomses /etc
COPY docker/purdue-af/configs/default.local /etc/cvmfs/
RUN mkdir -p "${HOME}/.globus" "${HOME}/.rnd" && \
    chown -R "${NB_USER}:${NB_GID}" "${HOME}/.globus" "${HOME}/.rnd" && \
    chmod 700 "${HOME}/.globus"

# Jupyter scripts and configs
COPY docker/purdue-af/jupyter/jupyter_server_config.py \
    docker/purdue-af/jupyter/docker_healthcheck.py \
    docker/purdue-af/scripts/eos-connect.sh \
    docker/purdue-af/jupyter/jupyter_config.json \
    docker/purdue-af/scripts/continue-config.yaml \
    /etc/jupyter/
COPY --chown=root:root docker/purdue-af/scripts/run-as-root.sh \
    docker/purdue-af/scripts/config-extensions.sh \
    docker/purdue-af/scripts/config-agents.sh \
    docker/purdue-af/scripts/create-symlinks.sh \
    /usr/local/bin/before-notebook.d/
COPY --chown=root:root \
    docker/purdue-af/jupyter/start.sh \
    docker/purdue-af/jupyter/start-notebook.sh \
    docker/purdue-af/jupyter/start-singleuser.sh \
    docker/purdue-af/scripts/upload-genaistudio-key \
    docker/purdue-af/scripts/delete-genaistudio-key \
    /usr/local/bin/
COPY --chown=root:root \
    docker/purdue-af/code-server/purdue-af-interface-controls \
    /opt/purdue-af/code-server/purdue-af-interface-controls/
# Agentic-interface skill: one source of truth in .claude/skills, adapted for
# in-session use at build time (the laptop setup preamble does not apply here).
COPY --chown=root:root .claude/skills/purdue-af-agentic-interface/SKILL.md \
    /tmp/skill/SKILL.md
COPY --chown=root:root docker/purdue-af/agents/platform-context.md \
    /opt/purdue-af/agents/platform-context.md
COPY --chown=root:root docker/purdue-af/scripts/managed-block.py \
    /usr/local/bin/managed-block.py
COPY --chown=root:root docker/purdue-af/scripts/prepare-skill.py /tmp/prepare-skill.py
# The pixi python, not the system one: this stage runs before ENV PATH below,
# and Rocky 8's /usr/bin/python3 is 3.6 — too old for these scripts.
RUN "${BASE_ENV_DIR}/bin/python3" /tmp/prepare-skill.py /tmp/skill/SKILL.md \
        /opt/purdue-af/skills/purdue-af-agentic-interface/SKILL.md && \
    rm -rf /tmp/skill /tmp/prepare-skill.py

COPY --chown=root:root docker/purdue-af/code-server/build-vsix.sh /usr/local/bin/build-paf-cs-vsix
RUN chmod +x /usr/local/bin/build-paf-cs-vsix && \
    build-paf-cs-vsix \
        /opt/purdue-af/code-server/purdue-af-interface-controls \
        /opt/purdue-af/code-server/purdue-af-interface-controls.vsix

RUN chmod +x /usr/local/bin/pixi && \
    chmod +x /usr/local/bin/* && \
    chmod +x /usr/local/bin/before-notebook.d/* && \
    fix-permissions /etc/jupyter/ && \
    fix-permissions "${HOME}"

# Set PATH before switching user so it's available to all users
ENV PATH="/usr/local/bin:/opt/npm-global/bin:/opt/pixi/.pixi/envs/base-env/bin:/opt/pixi/bin:${PATH}"

USER ${NB_USER}
WORKDIR "${HOME}"
EXPOSE 8888
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["start-notebook.sh"]
HEALTHCHECK --interval=5s --timeout=3s --start-period=5s --retries=3 \
    CMD /etc/jupyter/docker_healthcheck.py || exit 1

# --------------------------------------------------------------------------
# CI smoke stage — ci-images.yml builds `--target smoke` first, so the basic
# userland checks run DURING the build (no need to load the ~13 GB image
# into the runner's docker daemon just to `docker run` them). The CVMFS
# check needs the host's /cvmfs and lives in the e2e-pre-release job.
FROM runtime AS smoke
# start.sh SOURCES before-notebook.d/*.sh, so a top-level `exit` in any hook
# terminates the container before JupyterLab launches — a failure that only
# shows up at spawn time. Source the agent hook the same way start.sh does and
# prove the shell survives it. (config-extensions.sh is left out on purpose: it
# reaches the network to install code-server extensions.)
RUN NB_USER=jovyan NAMESPACE=cms bash -c \
        'source /usr/local/bin/before-notebook.d/config-agents.sh; echo HOOKS_SURVIVED' \
        >/tmp/hooks.log 2>&1; \
    cat /tmp/hooks.log; \
    grep -q HOOKS_SURVIVED /tmp/hooks.log || { \
        echo "FATAL: sourcing config-agents.sh exited the shell — start.sh would die" >&2; \
        exit 1; \
    }; \
    rm -f /tmp/hooks.log

RUN nvcc --version >/dev/null && \
    ps --version >/dev/null && \
    klist -V >/dev/null && \
    xz --version >/dev/null && \
    xrdcp --version >/dev/null 2>&1 && \
    claude --version >/dev/null && \
    codex --version >/dev/null && \
    python3 -c "import jupyterlab; print('smoke ok:', jupyterlab.__version__)"

# The published image: a bare alias of `runtime` so the smoke stage above
# stays opt-in — the DEFAULT target (CI push step)
# resolves here and carries no smoke layers.
FROM runtime
