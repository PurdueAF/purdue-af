local prometheus = import 'prometheus.libsonnet';
local panels = import 'panels.libsonnet';

{
  totalRunningPods:: panels.stat(
    targets=[
      prometheus.addQuery(
        'prometheus',
        'sum(count by (namespace)(kube_pod_labels{pod=~"purdue-af-.*", namespace=~"cms(-dev)?"}))',
        legendFormat='Current AF users'
      ),
    ],
    colorMode='value'
  ),

  totalRegisteredUsers:: panels.stat(
    targets=[
      prometheus.addQuery(
        'prometheus',
        'sum(jupyterhub_total_users{job="jupyterhub"})',
        legendFormat='Total registered users'
      ),
    ],
    colorMode='value'
  ),
}
