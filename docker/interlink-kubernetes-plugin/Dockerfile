# Purdue AF interLink Kubernetes plugin: the upstream image plus the site
# patches (README.md).

# Keep the default in sync with PLUGIN_REF; CI passes --build-arg explicitly.
ARG UPSTREAM_IMAGE=docker.io/mginfn/interlink-kubernetes-plugin:1.2.0

FROM ${UPSTREAM_IMAGE}

COPY docker/interlink-kubernetes-plugin/ensure-namespace.patch docker/interlink-kubernetes-plugin/wstunnel-client.patch docker/interlink-kubernetes-plugin/remote-placement.patch docker/interlink-kubernetes-plugin/remote-readiness.patch /tmp/

# The upstream image runs from source (WORKDIR /interlink-kubernetes-plugin);
# the check proves the patched modules parse, the options are wired, the sidecar builds
# and an affinity annotation's JSON becomes a typed V1Affinity.
RUN patch -p1 </tmp/ensure-namespace.patch && \
    patch -p1 </tmp/wstunnel-client.patch && \
    patch -p1 </tmp/remote-placement.patch && \
    patch -p1 -d / </tmp/remote-readiness.patch && \
    rm /tmp/ensure-namespace.patch /tmp/wstunnel-client.patch /tmp/remote-placement.patch /tmp/remote-readiness.patch && \
    python -c "from kubernetes import client as k; from app.common.config import Option; from app.entities import mappers; from app.services.kubernetes_plugin_service import KubernetesPluginService as S; assert Option.OFFLOADING_ENSURE_NAMESPACE and Option.WSTUNNEL_CLIENT_IMAGE and Option.OFFLOADING_RESTART_POLICY; spec = k.V1PodSpec(containers=[]); S._add_wstunnel_client_sidecar(None, spec, 'true', 'img'); assert spec.init_containers[0].restart_policy == 'Always'; a = mappers.deserialize_dict_to_k_model(k.ApiClient(), {'nodeAffinity': {'requiredDuringSchedulingIgnoredDuringExecution': {'nodeSelectorTerms': [{'matchExpressions': [{'key': 'k', 'operator': 'Gt', 'values': ['7']}]}]}}}, k.V1Affinity); assert a.node_affinity.required_during_scheduling_ignored_during_execution.node_selector_terms[0].match_expressions[0].operator == 'Gt'; assert {'startup_probe', 'liveness_probe', 'readiness_probe'} <= set(__import__('interlink').Container.model_fields); print('site patches applied')"

LABEL org.opencontainers.image.title="purdue-af-interlink-kubernetes-plugin" \
      org.opencontainers.image.description="interLink Kubernetes plugin with an optional namespace-ensure and a wstunnel client sidecar for least-privilege remote clusters" \
      org.opencontainers.image.source="https://github.com/PurdueAF/purdue-af"
