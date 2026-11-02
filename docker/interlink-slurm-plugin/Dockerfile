# Purdue AF interLink Slurm sidecar: one image for every RCAC cluster (README.md).

# CI builds from this default; keep it equal to PLUGIN_REF.
ARG SLURM_PLUGIN_REF=0.6.2-pre3
ARG GOLANG_IMAGE=golang:1.27
# EL8 for the site Slurm RPMs.
ARG BASE_IMAGE=docker.io/rockylinux/rockylinux:8

FROM ${GOLANG_IMAGE} AS build-stage

ARG SLURM_PLUGIN_REF
WORKDIR /app

RUN git clone --depth 1 --branch "${SLURM_PLUGIN_REF}" https://github.com/interlink-hq/interlink-slurm-plugin.git /app && \
    CGO_ENABLED=0 GOOS=linux go build -o bin/slurm-sidecar cmd/main.go

FROM ${BASE_IMAGE}

# Fix: https://github.com/hadolint/hadolint/wiki/DL4006 (SC3040)
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN dnf install -y \
        https://dl.fedoraproject.org/pub/epel/epel-release-latest-8.noarch.rpm \
        findutils cpio rpm munge && \
    dnf clean all && rm -rf /var/cache/dnf

# /etc/slurm and slurm-active/bin start empty, so an unset SLURM_CLUSTER reaches no controller.
COPY slurm/ /tmp/slurm-build/
RUN set -euo pipefail \
 && { getent group slurm >/dev/null || groupadd -g 1033 slurm; } \
 && { id -u slurm >/dev/null 2>&1 || adduser -u 2000 -g 1033 slurm; } \
 && mkdir -p /opt/purdue-af/slurm-configs \
             /opt/purdue-af/slurm-clients \
             /opt/purdue-af/slurm-active/bin \
             /var/log/slurm \
 && shopt -s nullglob \
 && printf 'ClusterName=smoke\nSlurmctldHost=localhost\n' > /tmp/smoke.conf \
 && rpms=(/tmp/slurm-build/slurm-*-1.el8.x86_64.rpm) \
 && { (( ${#rpms[@]} > 0 )) || { echo "no slurm/slurm-*-1.el8.x86_64.rpm in build context" >&2; exit 1; }; } \
 && for rpm in "${rpms[@]}"; do \
      ver="$(basename "${rpm}" | sed -n 's/^slurm-\(.*\)-1\.el8\.x86_64\.rpm$/\1/p')"; \
      [ -n "${ver}" ] || { echo "cannot parse version from ${rpm}" >&2; exit 1; }; \
      dest="/opt/purdue-af/slurm-clients/${ver}"; \
      mkdir -p "${dest}"; \
      rpm2cpio "${rpm}" | cpio -idm --quiet --directory="${dest}"; \
      test -x "${dest}/usr/bin/sbatch" \
        || { echo "extracted ${rpm} but ${dest}/usr/bin/sbatch missing" >&2; exit 1; }; \
      ln -sfn "${dest}/usr/lib64/slurm" /usr/lib64/slurm; \
      SLURM_CONF=/tmp/smoke.conf LD_LIBRARY_PATH="${dest}/usr/lib64" \
        "${dest}/usr/bin/sbatch" --version >/dev/null \
        || { echo "extracted ${rpm} but sbatch will not run — relocation broke RPATH/PluginDir" >&2; exit 1; }; \
      rm -f /usr/lib64/slurm; \
      echo "extracted Slurm client ${ver}"; \
    done \
 && test -f /tmp/slurm-build/client-versions \
 && cp /tmp/slurm-build/client-versions /opt/purdue-af/slurm-clients/client-versions \
 && while read -r cluster ver _; do \
      case "${cluster}" in ''|\#*) continue ;; esac; \
      [ -d "/opt/purdue-af/slurm-clients/${ver}" ] \
        || { echo "client-versions: ${cluster} -> ${ver} but RPM not extracted" >&2; exit 1; }; \
    done < /opt/purdue-af/slurm-clients/client-versions \
 && for d in /tmp/slurm-build/slurm-configs-*; do \
      [ -d "$d" ] || continue; \
      name="${d##*/slurm-configs-}"; \
      cp -a "$d" "/opt/purdue-af/slurm-configs/${name}" || exit 1; \
    done \
 && rm -rf /etc/slurm/* /tmp/slurm-build /tmp/smoke.conf \
 && chown -R slurm:slurm /var/log/slurm \
 && { test -n "$(find /opt/purdue-af/slurm-configs -mindepth 1 -maxdepth 1 -type d -print -quit)" \
      || { echo "no slurm/slurm-configs-* trees found in build context" >&2; exit 1; }; }

COPY docker/interlink-slurm-plugin/startup.sh /etc/startup.sh

RUN chmod 555 /etc/startup.sh

WORKDIR /root

COPY --from=build-stage /app/bin/slurm-sidecar /sidecar/slurm-sidecar

# The multi-cluster layout; munged needs the site key, so it stays down.
RUN test -x /sidecar/slurm-sidecar \
 && test -x /etc/startup.sh \
 && test -f /opt/purdue-af/slurm-configs/hammer/slurm.conf \
 && test -f /opt/purdue-af/slurm-clients/client-versions

# Active client binaries (symlinks filled by startup.sh) come first on PATH.
ENV PATH="/opt/purdue-af/slurm-active/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

LABEL org.opencontainers.image.title="purdue-af-interlink-slurm-plugin" \
      org.opencontainers.image.description="interLink Slurm sidecar with Purdue multi-cluster client configs" \
      org.opencontainers.image.source="https://github.com/PurdueAF/purdue-af"

ENTRYPOINT ["/bin/sh", "-c", "/etc/startup.sh && exec /sidecar/slurm-sidecar"]
