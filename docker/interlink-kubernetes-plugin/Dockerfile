# Purdue AF interLink Kubernetes plugin: the upstream image plus the
# ensure-namespace patch (README.md).

# Keep the default in sync with PLUGIN_REF; CI passes --build-arg explicitly.
ARG UPSTREAM_IMAGE=docker.io/mginfn/interlink-kubernetes-plugin:1.2.0

FROM ${UPSTREAM_IMAGE}

COPY docker/interlink-kubernetes-plugin/ensure-namespace.patch /tmp/

# The upstream image runs from source (WORKDIR /interlink-kubernetes-plugin);
# the import proves the patched modules parse and the new option is wired.
RUN patch -p1 </tmp/ensure-namespace.patch && \
    rm /tmp/ensure-namespace.patch && \
    python -c "from app.common.config import Option; from app.services import kubernetes_plugin_service; assert Option.OFFLOADING_ENSURE_NAMESPACE; print('ensure-namespace patch applied')"

LABEL org.opencontainers.image.title="purdue-af-interlink-kubernetes-plugin" \
      org.opencontainers.image.description="interLink Kubernetes plugin with an optional namespace-ensure for least-privilege remote clusters" \
      org.opencontainers.image.source="https://github.com/PurdueAF/purdue-af"
