local g = import 'github.com/grafana/grafonnet/gen/grafonnet-latest/main.libsonnet';

{
  totalRunningPods:: g.panel.stat.new('')
  + g.panel.stat.queryOptions.withTargets([
    g.query.prometheus.new(
      'prometheus',
      'sum(count by (namespace)(kube_pod_labels{pod=~"purdue-af-.*", namespace=~"cms(-dev)?"}))',
    )
    + g.query.prometheus.withLegendFormat('Current AF users')
    + g.query.prometheus.withInstant(),
  ])
  + g.panel.stat.options.withColorMode('value'),

  totalRegisteredUsers:: g.panel.stat.new('')
  + g.panel.stat.queryOptions.withTargets([
    g.query.prometheus.new(
      'prometheus',
      'sum(jupyterhub_total_users{job="jupyterhub"})',
    )
    + g.query.prometheus.withLegendFormat('Total registered users')
    + g.query.prometheus.withInstant(),
  ])
  + g.panel.stat.options.withColorMode('value'),

  deployedTritonLB:: g.panel.stat.new('')
  + g.panel.stat.queryOptions.withTargets([
    g.query.prometheus.new(
      'prometheus',
      'count(sum by (app) (nv_inference_count))',
    )
    + g.query.prometheus.withLegendFormat('Deployed load balancers')
    + g.query.prometheus.withInstant(),
  ])
  + g.panel.stat.options.withColorMode('value'),

  deployedTritonServers:: g.panel.stat.new('')
  + g.panel.stat.queryOptions.withTargets([
    g.query.prometheus.new(
      'prometheus',
      |||
        sum (
            kube_deployment_status_replicas_available{namespace="cms", deployment=~"triton-(.*)"}
        )
      |||,
    )
    + g.query.prometheus.withLegendFormat('Deployed Triton servers')
    + g.query.prometheus.withInstant(),
  ])
  + g.panel.stat.options.withColorMode('value'),

}