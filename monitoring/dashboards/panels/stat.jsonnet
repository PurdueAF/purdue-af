local prometheus = import 'prometheus.libsonnet';
local panels = import 'panels.libsonnet';

{
  totalRunningPods:: panels.stat(
    targets=[
      prometheus.addQuery(
        'prometheus',
        'sum(count by (namespace)(kube_pod_labels{pod=~"purdue-af-.*", namespace=~"cms(-dev)?"}))',
        legendFormat='Current AF users', instant = true
      ),
    ],
    colorMode='value'
  ),

  totalRegisteredUsers:: panels.stat(
    targets=[
      prometheus.addQuery(
        'prometheus',
        'sum(jupyterhub_total_users{job="jupyterhub"})',
        legendFormat='Total registered users',
        instant=true
      ),
    ],
    colorMode='value'
  ),

  deployedTritonLB:: panels.stat(
    targets=[
      prometheus.addQuery(
        'prometheus-rancher',
        'count(sum by (service) (nv_inference_count))',
        legendFormat='Deployed load balancers',
        instant=true
      ),
    ],
    colorMode='value'
  ),

  deployedTritonServers:: panels.stat(
    targets=[
      prometheus.addQuery(
        'prometheus',
        |||
          sum (
              kube_deployment_status_replicas_available{namespace="cms", deployment=~"triton(.*)", deployment!="triton-nginx"}
          )
        |||,
        legendFormat='Running Triton servers',
        instant=true
      ),
    ],
    colorMode='value'
  ),
}
