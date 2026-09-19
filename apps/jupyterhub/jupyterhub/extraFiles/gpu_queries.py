"""GPU availability queries shared by the Hub's profile form and the agentic
interface, so the two never disagree about how many GPUs are free.

Pure constants: importable as a module (``import gpu_queries``) and harmless
when z2jh execs it as a config snippet alongside the others.

Availability = allocatable - requested on schedulable (not cordoned) cms-af
nodes, from kube-state-metrics via Prometheus — the same data the Grafana
dashboards use, so no extra RBAC is needed anywhere.
"""

GPU_METRICS = {
    "nvidia.com/mig-1g.5gb": "nvidia_com_mig_1g_5gb",
    "nvidia.com/mig-7g.40gb": "nvidia_com_mig_7g_40gb",
    "nvidia.com/gpu": "nvidia_com_gpu",
}

# Only schedulable AF nodes count: tainted cms-af and not cordoned.
_NODE_SCOPE = (
    " * on (node) group_left() (kube_node_spec_unschedulable == bool 0)"
    ' * on (node) group_left() group by (node) (kube_node_spec_taint{value="cms-af"})'
)
_GPU_RESOURCE = 'resource=~"nvidia_com_(mig_.+|gpu)"'
ALLOC_QUERY = (
    "sum by (resource) ("
    "kube_node_status_allocatable{" + _GPU_RESOURCE + "}" + _NODE_SCOPE + ")"
)
# Finished pods keep their request series; count only Pending and Running.
USED_QUERY = (
    "sum by (resource) ("
    "kube_pod_container_resource_requests{"
    + _GPU_RESOURCE
    + "}"
    + _NODE_SCOPE
    + " * on (namespace, pod) group_left() (max by (namespace, pod) "
    '(kube_pod_status_phase{phase=~"Pending|Running"}) == bool 1)'
    ")"
)
