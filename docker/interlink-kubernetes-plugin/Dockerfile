# Purdue AF interLink Kubernetes plugin: the upstream image plus the site
# patches (README.md).

# CI builds from this default; keep it equal to PLUGIN_REF.
ARG UPSTREAM_IMAGE=docker.io/mginfn/interlink-kubernetes-plugin:1.2.0

FROM ${UPSTREAM_IMAGE}

# The upstream image runs from source; each patch applies on top of those before it.
RUN --mount=type=bind,source=docker/interlink-kubernetes-plugin,target=/patches \
    patch -p1 --fuzz=0 -d / </patches/01-ensure-namespace.patch && \
    patch -p1 --fuzz=0 -d / </patches/02-wstunnel-client.patch && \
    patch -p1 --fuzz=0 -d / </patches/03-remote-placement.patch && \
    patch -p1 --fuzz=0 -d / </patches/04-remote-readiness.patch && \
    patch -p1 --fuzz=0 -d / </patches/05-remote-pending.patch && \
    patch -p1 --fuzz=0 -d / </patches/06-remote-gone.patch && \
    patch -p1 --fuzz=0 -d / </patches/07-remote-drain.patch && \
    python - </patches/verify-patches.py

LABEL org.opencontainers.image.title="purdue-af-interlink-kubernetes-plugin" \
      org.opencontainers.image.description="interLink Kubernetes plugin with the Purdue site patches" \
      org.opencontainers.image.source="https://github.com/PurdueAF/purdue-af"
