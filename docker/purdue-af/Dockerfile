# Copyright (c) Jupyter Development Team.
# Distributed under the terms of the Modified BSD License.
# Stages: system -> stack -> runtime (+ smoke, CI only).

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

# --disablerepo=cuda: the base's cuda.repo is NVIDIA's rolling repo, whose CUDA 13 obsoletes the pinned 12.4.
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
      # absent from the CUDA base: ps, kinit/klist (eos-connect.sh), gcc -latomic
      procps-ng psmisc krb5-workstation xz cpio libatomic \
 && dnf -y --disablerepo=cuda --enablerepo=epel install tini \
 # the only cuda-repo install: an exact version cannot pull obsoletes
 && dnf -y install libcudnn8-8.9.7.29-1+cuda12.2 \
 && dnf clean all \
 && ln -s /usr/bin/python3 /usr/bin/python \
 && rm -rf /var/cache/dnf \
 && rm -f /usr/bin/vi && ln -s /usr/bin/vim /usr/bin/vi \
 && localedef -c -f UTF-8 -i en_US en_US.UTF-8

# CERN.CH realm + CA defaults for eos-connect.sh's kinit.
COPY docker/purdue-af/configs/krb5.conf.d/ /etc/krb5.conf.d/
COPY docker/purdue-af/configs/certs/ /etc/pki/tls/certs/

COPY docker/purdue-af/osg/igtf-ca-certs-1.136-1.osg24.el8.noarch.rpm docker/purdue-af/osg/vo-client-138-2.osg24.el8.noarch.rpm /tmp/
RUN dnf -y --disablerepo='*' install --nogpgcheck --setopt=install_weak_deps=False \
        /tmp/igtf-ca-certs-1.136-1.osg24.el8.noarch.rpm \
        /tmp/vo-client-138-2.osg24.el8.noarch.rpm && \
     rm -f /tmp/*.rpm && dnf clean all && rm -rf /var/cache/dnf

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

ENV SHELL=/bin/bash \
    NB_USER="${NB_USER}" \
    NB_UID=${NB_UID} \
    NB_GID=${NB_GID} \
    LANG=en_US.UTF-8 \
    LANGUAGE=en_US.UTF-8 \
    LC_ALL=en_US.UTF-8 \
    HOME="/home/${NB_USER}"

COPY --chown=root:root docker/purdue-af/scripts/fix-permissions /usr/local/bin/fix-permissions
RUN chmod a+rx /usr/local/bin/fix-permissions

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

ARG PIXI_VERSION="0.62.2" # read by ci-pixi-*.yml

ENV PIXI_HOME="/opt/pixi" \
    BASE_ENV_DIR="/opt/pixi/.pixi/envs/base-env"

WORKDIR "${PIXI_HOME}"
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

# Coding-agent CLIs in their own prefix, out of reach of a pixi rebuild. jupyter-ai's
# Claude/Codex personas appear only when the *-acp adapters are on PATH.
ARG CLAUDE_CODE_VERSION="2.1.281"
ARG CODEX_VERSION="0.155.1"
ARG OPENCODE_VERSION="1.18.32"
ARG CLAUDE_AGENT_ACP_VERSION="0.81.2"
ARG CODEX_ACP_VERSION="0.16.0"
ENV NPM_GLOBAL="/opt/npm-global"
# hadolint ignore=DL3016  # versions ARE pinned, via the ARGs above
RUN PATH="${BASE_ENV_DIR}/bin:${PATH}" \
    npm install -g --prefix "${NPM_GLOBAL}" --no-fund --no-audit \
        "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
        "@openai/codex@${CODEX_VERSION}" \
        "opencode-ai@${OPENCODE_VERSION}" \
        "@agentclientprotocol/claude-agent-acp@${CLAUDE_AGENT_ACP_VERSION}" \
        "@zed-industries/codex-acp@${CODEX_ACP_VERSION}" && \
    PATH="${NPM_GLOBAL}/bin:${BASE_ENV_DIR}/bin:${PATH}" claude --version && \
    PATH="${NPM_GLOBAL}/bin:${BASE_ENV_DIR}/bin:${PATH}" codex --version && \
    PATH="${NPM_GLOBAL}/bin:${BASE_ENV_DIR}/bin:${PATH}" opencode --version && \
    rm -rf "${HOME}/.npm" || true

COPY --chown=root:root pixi/check-env.py /tmp/check-env.py
RUN "${BASE_ENV_DIR}/bin/python" /tmp/check-env.py \
        --manifest "${PIXI_HOME}/pixi.toml" --env base-env && \
    rm -f /tmp/check-env.py

# ------------------------------------------------------------
# Stage: runtime (lightweight; COPY only, no builds or installation routines)
# ------------------------------------------------------------
FROM system AS runtime

# ARGs are not inherited across stages.
ARG NB_USER="jovyan"

# NVIDIA_VISIBLE_DEVICES=all is inherited on purpose; the hub's modify_pod_hook sets void for 0-GPU sessions.
ENV PIXI_HOME="/opt/pixi" \
    BASE_ENV_DIR="/opt/pixi/.pixi/envs/base-env" \
    X509_CERT_DIR=/cvmfs/cms.cern.ch/grid/etc/grid-security/certificates

COPY --from=stack /opt/pixi /opt/pixi

# The CLIs' shebangs resolve `node` from PATH (the base env, below).
COPY --from=stack /opt/npm-global /opt/npm-global

COPY --chown=root:root docker/purdue-af/pixi-wrapper /usr/local/bin/pixi

RUN mkdir -p /etc/cvmfs /etc/cvmfs/SITECONF/JobConfig/ /etc/cvmfs/SITECONF/PhEDEx
COPY docker/purdue-af/xml/site-local-config.xml /etc/cvmfs/SITECONF/JobConfig/
COPY docker/purdue-af/xml/storage.xml /etc/cvmfs/SITECONF/PhEDEx/
COPY docker/purdue-af/configs/vomses /etc
COPY docker/purdue-af/configs/default.local /etc/cvmfs/
RUN mkdir -p "${HOME}/.globus" "${HOME}/.rnd" && \
    chown -R "${NB_USER}:${NB_GID}" "${HOME}/.globus" "${HOME}/.rnd" && \
    chmod 700 "${HOME}/.globus"

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
    docker/purdue-af/scripts/clean-session-state.sh \
    /usr/local/bin/before-notebook.d/
# A library for the hooks, so not in before-notebook.d.
COPY --chown=root:root docker/purdue-af/scripts/af-as-user.sh /usr/local/bin/
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
COPY --chown=root:root .claude/skills/purdue-af-agentic-interface/SKILL.md \
    /opt/purdue-af/skills/purdue-af-agentic-interface/SKILL.md
COPY --chown=root:root docker/purdue-af/agents/platform-context.md \
    /opt/purdue-af/agents/platform-context.md
COPY --chown=root:root docker/purdue-af/scripts/managed-block.py \
    /usr/local/bin/managed-block.py

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

ENV PATH="/usr/local/bin:/opt/npm-global/bin:/opt/pixi/.pixi/envs/base-env/bin:/opt/pixi/bin:${PATH}"

USER ${NB_USER}
WORKDIR "${HOME}"
EXPOSE 8888
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["start-notebook.sh"]
HEALTHCHECK --interval=5s --timeout=3s --start-period=5s --retries=3 \
    CMD /etc/jupyter/docker_healthcheck.py || exit 1

# --------------------------------------------------------------------------
# Build-time checks, built by ci-images.yml with `--target smoke`.
FROM runtime AS smoke
# Hooks are sourced as start.sh sources them; config-extensions.sh is skipped (network).
RUN NB_USER=jovyan NAMESPACE=cms bash -c \
        'source /usr/local/bin/before-notebook.d/config-agents.sh; echo HOOKS_SURVIVED' \
        >/tmp/hooks.log 2>&1; \
    cat /tmp/hooks.log; \
    grep -q HOOKS_SURVIVED /tmp/hooks.log || { \
        echo "FATAL: sourcing config-agents.sh exited the shell — start.sh would die" >&2; \
        exit 1; \
    }; \
    rm -f /tmp/hooks.log
RUN NB_USER=jovyan bash -c \
        'set -e; source /usr/local/bin/before-notebook.d/clean-session-state.sh; echo HOOKS_SURVIVED' \
        >/tmp/hooks.log 2>&1; \
    cat /tmp/hooks.log; \
    grep -q HOOKS_SURVIVED /tmp/hooks.log || { \
        echo "FATAL: sourcing clean-session-state.sh exited the shell — start.sh would die" >&2; \
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
    opencode --version >/dev/null && \
    command -v claude-agent-acp >/dev/null && \
    command -v codex-acp >/dev/null && \
    python3 -c "import jupyterlab; print('smoke ok:', jupyterlab.__version__)" && \
    python3 -c "import jupyter_ai; print('smoke ok: jupyter-ai present')"

# Default target: the published image, without smoke layers.
FROM runtime
