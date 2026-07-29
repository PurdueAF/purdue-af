# Purdue AF interLink Slurm sidecar — one image, many RCAC clusters.
#
# Cluster identity is NOT baked into /etc/slurm at build time. Every
# slurm/slurm-configs-<cluster>/ tree in the repo is installed under
# /opt/purdue-af/slurm-configs/<cluster>/; startup.sh copies the tree named
# by $SLURM_CLUSTER into /etc/slurm before starting munged + the sidecar.
#
# Version tag == upstream plugin checkout == PLUGIN_REF (single source of
# truth). CI publishes ghcr.io/purdueaf/interlink-slurm-plugin:$PLUGIN_REF;
# values pin that same tag via ghcr-proxy-cache. Bumping the plugin means
# editing PLUGIN_REF only — the Dockerfile ARG default, the image tag, and
# the values pins are asserted equal in tests.
#
# Adding a cluster:
#   1. Drop client configs at slurm/slurm-configs-<name>/ (must include slurm.conf)
#   2. Set plugin.envs SLURM_CLUSTER=<name> in apps/interlink/<name>/values.yaml
#   3. Mount munge-key-<name> (already the HelmRelease postRenderer convention)
#   4. Merge to main — CI rebuilds (image-inputs covers slurm/) and retags $PLUGIN_REF

# Keep the default in sync with PLUGIN_REF; CI passes --build-arg explicitly.
ARG SLURM_PLUGIN_REF=0.6.2-pre3
ARG GOLANG_IMAGE=golang:1.26
# Same EL8 CUDA base as docker/purdue-af (Alma/Rocky-compatible for the site RPM).
ARG BASE_IMAGE=geddes-registry.rcac.purdue.edu/docker-hub-cache/nvidia/cuda:12.4.1-devel-rockylinux8

FROM ${GOLANG_IMAGE} AS build-stage

ARG SLURM_PLUGIN_REF
WORKDIR /app

RUN git clone https://github.com/interlink-hq/interlink-slurm-plugin.git /app && \
    git fetch --tags && \
    git checkout "${SLURM_PLUGIN_REF}" && \
    CGO_ENABLED=0 GOOS=linux go build -o bin/slurm-sidecar cmd/main.go

FROM ${BASE_IMAGE}

# Fix: https://github.com/hadolint/hadolint/wiki/DL4006 (SC3040)
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Runtime deps for munge + the Slurm client tools in the site RPM.
RUN dnf install -y \
        https://dl.fedoraproject.org/pub/epel/epel-release-latest-8.noarch.rpm \
        wget sudo gcc make openssl-devel bzip2-devel zlib-devel diffutils \
        findutils && \
    dnf clean all && rm -rf /var/cache/dnf

# Site Slurm client RPM + every per-cluster config tree. The RPM may drop a
# default /etc/slurm; wipe it so a missing SLURM_CLUSTER cannot silently talk
# to the wrong controller.
COPY slurm/ /tmp/slurm-build/
RUN set -euo pipefail \
 && rpm=/tmp/slurm-build/slurm-25.11.4-1.el8.x86_64.rpm \
 && test -f "${rpm}" \
 && dnf localinstall -y "${rpm}" \
 && groupadd -g 1033 slurm || true \
 && adduser -u 2000 -g 1033 slurm || true \
 && mkdir -p /opt/purdue-af/slurm-configs /var/log/slurm \
 && for d in /tmp/slurm-build/slurm-configs-*; do \
      [ -d "$d" ] || continue; \
      name="${d##*/slurm-configs-}"; \
      cp -a "$d" "/opt/purdue-af/slurm-configs/${name}"; \
    done \
 && rm -rf /etc/slurm/* /tmp/slurm-build \
 && chown -R slurm:slurm /var/log/slurm \
 && dnf clean all && rm -rf /var/cache/dnf \
 && ls -1 /opt/purdue-af/slurm-configs | grep -q . \
 || { echo "no slurm/slurm-configs-* trees found in build context" >&2; exit 1; }

COPY docker/interlink-slurm-plugin/startup.sh /etc/startup.sh

RUN chmod 555 /etc/startup.sh && \
    mkdir -p /cvmfs/grid.cern.ch/etc/grid-security

WORKDIR /root

COPY --from=build-stage /app/bin/slurm-sidecar /sidecar/slurm-sidecar

ENV SLURMCONFIGPATH=/root/SlurmConfig.yaml
# Must be overridden per Deployment (hammer, gautschi, …).
ENV SLURM_CLUSTER=

LABEL org.opencontainers.image.title="purdue-af-interlink-slurm-plugin" \
      org.opencontainers.image.description="interLink Slurm sidecar with Purdue multi-cluster client configs" \
      org.opencontainers.image.source="https://github.com/PurdueAF/purdue-af"

ENTRYPOINT ["/bin/sh", "-c", "/etc/startup.sh && SHARED_FS=true /sidecar/slurm-sidecar "]
