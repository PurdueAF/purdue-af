"""Run by the image build after the site patches, from the plugin's source tree."""

from app.common.config import Option
from app.entities import mappers
from app.services.kubernetes_plugin_service import KubernetesPluginService
from interlink import Container
from kubernetes import client

assert Option.OFFLOADING_ENSURE_NAMESPACE
assert Option.WSTUNNEL_CLIENT_IMAGE
assert Option.OFFLOADING_RESTART_POLICY
assert Option.OFFLOADING_DELETE_DELAY_SECONDS

spec = client.V1PodSpec(containers=[])
KubernetesPluginService._add_wstunnel_client_sidecar(None, spec, "true", "img")
assert spec.init_containers[0].restart_policy == "Always"

term = {"matchExpressions": [{"key": "k", "operator": "Gt", "values": ["7"]}]}
affinity = mappers.deserialize_dict_to_k_model(
    client.ApiClient(),
    {
        "nodeAffinity": {
            "requiredDuringSchedulingIgnoredDuringExecution": {
                "nodeSelectorTerms": [term]
            }
        }
    },
    client.V1Affinity,
)
required = affinity.node_affinity.required_during_scheduling_ignored_during_execution
assert required.node_selector_terms[0].match_expressions[0].operator == "Gt"

assert {"startup_probe", "liveness_probe", "readiness_probe"} <= set(
    Container.model_fields
)

print("site patches applied")
