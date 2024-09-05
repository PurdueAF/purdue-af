local g = import 'github.com/grafana/grafonnet/gen/grafonnet-latest/main.libsonnet';
local prometheus = import 'prometheus.libsonnet';
local panels = import 'panels.libsonnet';

local w = g.panel.table.gridPos.withW;
local h = g.panel.table.gridPos.withH;

local user =
  g.dashboard.variable.query.new(
    'user',
    query='query_result(af_home_dir_util{namespace=~"cms",pod=~"purdue-af-.*"})'
  )
  + g.dashboard.variable.query.withRegex(
    '/username="(?<text>[^"]+)|pod="(?<value>[^"]+)/g'
  )
  + g.dashboard.variable.query.withDatasource(
    type='prometheus',
    uid='prometheus',
  )
  + g.dashboard.variable.query.withSort(1)
  + g.dashboard.variable.query.selectionOptions.withIncludeAll()
;

local userResourceUtil = panels.timeSeries(
  title='Resource usage history',
  targets=[
    prometheus.addQuery(
      'prometheus-rancher',
      |||
        sum(
            irate(container_cpu_usage_seconds_total{namespace=~"cms",pod="$user", container="notebook"}[5m])
        ) by (pod)
            /
        sum by (pod)(
            kube_pod_container_resource_requests{namespace=~"cms",pod="$user", resource="cpu", container="notebook"}
        )
      |||,
      legendFormat='CPU utilization'
    ),
    prometheus.addQuery(
      'prometheus-rancher',
      |||
        sum by (pod)(
            container_memory_working_set_bytes{namespace=~"cms", pod=~"$user", container="notebook"}
        ) /
        sum by (pod)(
            kube_pod_container_resource_requests{namespace=~"cms", pod=~"$user", resource="memory", container="notebook"}
        )
      |||,
      legendFormat='Memory utilization'
    ),
    prometheus.addQuery(
      'prometheus-rancher',
      |||
        sum by (pod) (DCGM_FI_PROF_GR_ENGINE_ACTIVE{kubernetes_node=~"geddes-g00.*",pod=~"$user"})
      |||,
      legendFormat='GPU engine utilization'
    ),
    prometheus.addQuery(
      'prometheus-rancher',
      |||
        sum by (pod) (
          ( DCGM_FI_DEV_FB_USED{kubernetes_node=~"geddes-g00.*",pod=~"$user"}
            / ( 
              DCGM_FI_DEV_FB_USED{kubernetes_node=~"geddes-g00.*",pod=~"$user"} +
              DCGM_FI_DEV_FB_FREE{kubernetes_node=~"geddes-g00.*",pod=~"$user"}
            )
          )
        )
      |||,
      legendFormat='GPU memory utilization'
    ),
    prometheus.addQuery(
      'prometheus',
      'sum(af_home_dir_util{namespace=~"cms",job="af-pod-monitor",pod=~"$user"})',
      legendFormat='/home/ storage utilization'
    ),
    prometheus.addQuery(
      'prometheus-rancher',
      '
        sum by (pod) (
          irate(container_network_transmit_bytes_total{namespace="cms", pod=~"$user"}[5m])
        )
      ',
      legendFormat='I/O send'
    ),
    prometheus.addQuery(
      'prometheus-rancher',
      '
        sum by (pod) (
          irate(container_network_receive_bytes_total{namespace="cms", pod=~"$user"}[5m])
        )
      ',
      legendFormat='I/O receive'
    ),
  ],
  unit='percentunit',
  min=0,
  legendPlacement='right',
)  + g.panel.timeSeries.standardOptions.withOverrides([
  g.panel.timeSeries.fieldOverride.byRegexp.new("I/O.*")
    + g.panel.timeSeries.fieldOverride.byName.withPropertiesFromOptions(
      g.panel.timeSeries.fieldConfig.defaults.custom.withAxisPlacement("right")
      + g.panel.table.standardOptions.withUnit("binBps")
  ),
]);

local userResourceUtilAbs = panels.timeSeries(
  title='Resource usage history',
  targets=[
    prometheus.addQuery(
      'prometheus-rancher',
      |||
        sum(
            irate(container_cpu_usage_seconds_total{namespace=~"cms",pod="$user", container="notebook"}[5m])
        ) by (pod)
      |||,
      legendFormat='CPU utilization'
    ),
    prometheus.addQuery(
      'prometheus-rancher',
      |||
        sum by (pod)(
            container_memory_working_set_bytes{namespace=~"cms", pod=~"$user", container="notebook"}
        )
      |||,
      legendFormat='Memory utilization'
    ),
  ],
  unit='cpu',
  min=0,
  legendPlacement='right',
)  + g.panel.timeSeries.standardOptions.withOverrides([
  g.panel.timeSeries.fieldOverride.byRegexp.new("Memory.*")
    + g.panel.timeSeries.fieldOverride.byName.withPropertiesFromOptions(
      g.panel.timeSeries.fieldConfig.defaults.custom.withAxisPlacement("right")
      + g.panel.table.standardOptions.withUnit("bytes")
  ),
]);

g.dashboard.new('Single User Statistics')
+ g.dashboard.withVariables([
  user
])
+ g.dashboard.withUid('single-user-stat-dashboard')
+ g.dashboard.withDescription('Purdue AF Single User Statistics')
+ g.dashboard.withLiveNow()
+ g.dashboard.withRefresh('10s')
+ g.dashboard.withTimezone(value="browser")
+ g.dashboard.graphTooltip.withSharedCrosshair()
+ g.dashboard.withPanels([
    userResourceUtil + w(16) + h(16),
    g.panel.row.new(''),
    userResourceUtilAbs + w(10) + h(10),
    g.panel.row.new(''),
    g.panel.row.new(''),
    g.panel.row.new(''),

])
+ {
  theme: 'light'
}