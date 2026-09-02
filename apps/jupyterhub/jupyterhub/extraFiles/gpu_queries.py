"""GPU availability queries shared by the Hub's profile form and the agentic
interface, so the two never disagree about how many GPUs are free.

Pure constants: importable as a module (``import gpu_queries``) and harmless
when z2jh execs it as a config snippet alongside the others.

Availability = allocatable - requested on schedulable (not cordoned) cms-af
nodes, from kube-state-metrics via Prometheus — the same data the Grafana
dashboards use, so no extra RBAC is needed anywhere.
"""

# k8s extended GPU resource -> kube-state-metrics resource label. Covers MIG
# slices (A100) and whole GPUs (T4 via nvidia.com/gpu).
GPU_METRICS = {
    "nvidia.com/mig-1g.5gb": "nvidia_com_mig_1g_5gb",
    "nvidia.com/mig-7g.40gb": "nvidia_com_mig_7g_40gb",
    "nvidia.com/gpu": "nvidia_com_gpu",
}

# Scope both sides of the subtraction to schedulable AF nodes: tainted cms-af
# and not cordoned ("== bool" / "group by" turn the join vectors into 0/1
# weights). Pods on a cordoned node keep running but neither they nor the
# node's capacity count towards what a new pod can be scheduled on.
_NODE_SCOPE = (
    " * on (node) group_left() (kube_node_spec_unschedulable == bool 0)"
    ' * on (node) group_left() group by (node) (kube_node_spec_taint{value="cms-af"})'
)
_GPU_RESOURCE = 'resource=~"nvidia_com_(mig_.+|gpu)"'
ALLOC_QUERY = (
    "sum by (resource) ("
    "kube_node_status_allocatable{" + _GPU_RESOURCE + "}" + _NODE_SCOPE + ")"
)
# Completed/failed pods keep their kube-state-metrics request series, so only
# count pods that are currently Pending or Running.
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
