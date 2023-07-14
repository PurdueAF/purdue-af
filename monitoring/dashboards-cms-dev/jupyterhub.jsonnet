#!/usr/bin/env jsonnet -J ../vendor
// Deploys one dashboard - "JupyterHub dashboard",
// with useful stats about usage & diagnostics.
local grafana = import 'grafonnet/grafana.libsonnet';
local dashboard = grafana.dashboard;
local singlestat = grafana.singlestat;
local graphPanel = grafana.graphPanel;
local gaugePanel = grafana.gaugePanel;
local prometheus = grafana.prometheus;
local template = grafana.template;
local tablePanel = grafana.tablePanel;
local row = grafana.row;
local heatmapPanel = grafana.heatmapPanel;

local jupyterhub = import 'jupyterhub.libsonnet';
local standardDims = jupyterhub.standardDims;

local templates = [
  template.datasource(
    name='PROMETHEUS_DS',
    query='prometheus',
    current=null,
    hide='label',
  ),
  // template.new(
  //   'hub',
  //   datasource='$PROMETHEUS_DS',
  //   query='label_values(kube_service_labels{service="hub"}, namespace)',
  //   // Allow viewing dashboard for multiple combined hubs
  //   includeAll=true,
  //   multi=true
  // ),
];


// Hub usage stats
local currentRunningAFpodsSingle = singlestat.new(
  'Currently running AF pods',
  decimals=0,
  valueName="users",
  valueFontSize="50%",
  postfix=' users',
  // maxPerRow=2,
  transparent=true,
  colorValue=true,
  datasource='$PROMETHEUS_DS'
).addTarget(
  prometheus.target(
    |||
      count by (namespace)(kube_pod_labels{pod=~"purdue-af-.*", namespace=~"cms(-dev)?"})
    |||,
    legendFormat='Users in {{namespace}}',
  ),
);

local currentRunningAFpods = graphPanel.new(
  'Currently running AF pods',
  decimals=0,
  stack=false,
  min=0,
  datasource='$PROMETHEUS_DS'
).addTargets([
  prometheus.target(
    |||
      count by (namespace)(kube_pod_labels{pod=~"purdue-af-.*", namespace=~"cms(-dev)?"})
    |||,
    legendFormat='{{namespace}}',
  ),
]);

local usersPerNode = graphPanel.new(
  'Users per node',
  decimals=0,
  min=0,
  datasource='$PROMETHEUS_DS'
).addTargets([
  prometheus.target(
    |||
      count by (node)(kube_pod_info{namespace=~"cms(-dev)?", node!="", pod=~"purdue-af-.*"})
    |||,
    legendFormat='{{ node }}'
  ),
]);


local podAgeDistribution = heatmapPanel.new(
  'Age distribution of running AF pods (dev instance)',
  // xBucketSize and interval must match to get correct values out of heatmaps
  xBucketSize='600s',
  yAxis_format='s',
  yAxis_min=0,
  color_colorScheme='interpolateViridis',
  datasource='$PROMETHEUS_DS'
).addTargets([
  prometheus.target(
    |||
      (
        time()
        - (
          kube_pod_created{pod=~"purdue-af-.*", namespace=~"cms(-dev)?"}
        )
      )
    |||,
    interval='600s',
    intervalFactor=1,
  ),
]);

local hubResponseLatencyDev = graphPanel.new(
  'Hub response latency (dev instance)',
  formatY1='s',
  min=0,
  datasource='$PROMETHEUS_DS'
).addTargets([
  prometheus.target(
    |||
      histogram_quantile(
        0.99,
        sum(
          rate(
            jupyterhub_request_duration_seconds_bucket{
              job="jupyterhub",
              instance="cmsdev.geddes.rcac.purdue.edu:80",
              # Ignore SpawnProgressAPIHandler, as it is a EventSource stream
              # and keeps long lived connections open
              handler!="jupyterhub.apihandlers.users.SpawnProgressAPIHandler"
            }[5m]
          )
        ) by (le))
    |||,
    legendFormat='99th percentile'
  ),
  prometheus.target(
    |||
      histogram_quantile(
        0.50,
        sum(
          rate(
            jupyterhub_request_duration_seconds_bucket{
              job="jupyterhub",
              instance="cmsdev.geddes.rcac.purdue.edu:80",
              # Ignore SpawnProgressAPIHandler, as it is a EventSource stream
              # and keeps long lived connections open
              handler!="jupyterhub.apihandlers.users.SpawnProgressAPIHandler"
            }[5m]
          )
        ) by (le))
    |||,
    legendFormat='50th percentile'
  ),
  prometheus.target(
    |||
      histogram_quantile(
        0.25,
        sum(
          rate(
            jupyterhub_request_duration_seconds_bucket{
              job="jupyterhub",
              instance="cmsdev.geddes.rcac.purdue.edu:80",
              # Ignore SpawnProgressAPIHandler, as it is a EventSource stream
              # and keeps long lived connections open
              handler!="jupyterhub.apihandlers.users.SpawnProgressAPIHandler"
            }[5m]
          )
        ) by (le))
    |||,
    legendFormat='25th percentile'
  ),
]);

local hubResponseCodesDev = graphPanel.new(
  'Hub response status codes',
  min=0,
  datasource='$PROMETHEUS_DS'
).addTargets([
  prometheus.target(
    |||
      sum(
        increase(
          jupyterhub_request_duration_seconds_bucket{
            job="jupyterhub",
            instance="cmsdev.geddes.rcac.purdue.edu:80",
          }[2m]
        )
      ) by (code)
    |||,
    legendFormat='{{ code }}'
  ),
]);

local serverStartTimesDev = graphPanel.new(
  'Server Start Times',
  formatY1='s',
  lines=false,
  min=0,
  points=true,
  pointradius=2,
  datasource='$PROMETHEUS_DS'
).addTargets([
  prometheus.target(
    // Metrics from hub seems to have `namespace` rather than just `namespace`
    'histogram_quantile(0.99, sum(rate(jupyterhub_server_spawn_duration_seconds_bucket{
      job="jupyterhub",
      instance="cmsdev.geddes.rcac.purdue.edu:80"
    }[5m])) by (le))',
    legendFormat='99th percentile'
  ),
  prometheus.target(
    'histogram_quantile(0.5, sum(rate(jupyterhub_server_spawn_duration_seconds_bucket{
      job="jupyterhub",
      instance="cmsdev.geddes.rcac.purdue.edu:80",
    }[5m])) by (le))',
    legendFormat='50th percentile'
  ),
]);

local nodeCpuUtil = graphPanel.new(
  '',
  // 'Node CPU Utilization %',
  formatY1='percentunit',
  description=|||
    % of available CPUs currently in use
  |||,
  min=0,
  // since this is actual measured utilization, it should not be able to exceed max=1
  // max=1,
  datasource='$PROMETHEUS_DS',
  legend_rightSide=true,
  transparent=true
).addTargets([
  prometheus.target(
    |||
      sum by (node)(
        label_replace(
          rate(node_cpu_seconds_total{mode!="idle"}[1m]),
          "node", "$1", "instance", "(.*).rcac.purdue.edu:9796"
        )
      )
      /
      sum(kube_node_status_capacity{resource="cpu"}) by (node)
    |||,
    legendFormat='{{ node }}'
  ),
]);

local nodeMemoryUtil = graphPanel.new(
  '',
  // 'Node Memory Utilization %',
  formatY1='percentunit',
  description=|||
    % of available Memory currently in use
  |||,
  min=0,
  // since this is actual measured utilization, it should not be able to exceed max=1
  // max=1,
  datasource='$PROMETHEUS_DS',
  legend_rightSide=true,
  transparent=true
).addTargets([
  prometheus.target(
    |||
      label_replace(
        1 - (
          sum (
            # Memory that can be allocated to processes when they need
            node_memory_MemFree_bytes + # Unused bytes
            node_memory_Cached_bytes + # Shared memory + temporary disk cache
            node_memory_Buffers_bytes # Very temporary buffer memory cache for disk i/o
          ) by (instance)
          /
          sum(node_memory_MemTotal_bytes) by (instance)
        ),
        "node", "$1", "instance", "(.*).rcac.purdue.edu:9796"
      )
    |||,
    legendFormat='{{node}}'
  ),
]);


local nodeCpuUtilGauge = gaugePanel.new(
  'Node CPU Utilization %',
  datasource='$PROMETHEUS_DS',
  description=|||
    % of available CPUs currently in use
  |||,
  min=0,
  max=1,
  showThresholdLabels=false,
  thresholdsMode='percentage',
  unit='percentunit',
  transparent=true,
).addTargets([
  prometheus.target(
    |||
      label_replace(
        sum by (node)(
          label_replace(
            rate(node_cpu_seconds_total{mode!="idle"}[1m]),
            "node", "$1", "instance", "(.*).rcac.purdue.edu:9796"
          )
        )
        /
        sum(kube_node_status_capacity{resource="cpu"}) by (node),
        "metric", "CPU", "node", "(.+)"
      )
    |||,
    legendFormat='{{ node }}'
  ),
]).addThresholds(
  [
    { color: 'green', value: 0.0},
    { color: 'yellow', value: 60},
    { color: 'orange', value: 80 },
    { color: 'red', value: 90 },
    ]
);

local nodeMemUtilGauge = gaugePanel.new(
  'Node Memory Utilization %',
  datasource='$PROMETHEUS_DS',
  description=|||
    % of available CPUs currently in use
  |||,
  min=0,
  max=1,
  showThresholdLabels=false,
  thresholdsMode='percentage',
  unit='percentunit',
  transparent=true,
).addTargets([
  prometheus.target(
    |||
      label_replace(
        label_replace(
          1 - (
            sum (
              # Memory that can be allocated to processes when they need
              node_memory_MemFree_bytes + # Unused bytes
              node_memory_Cached_bytes + # Shared memory + temporary disk cache
              node_memory_Buffers_bytes # Very temporary buffer memory cache for disk i/o
            ) by (instance)
            /
            sum(node_memory_MemTotal_bytes) by (instance)
          ),
          "node", "$1", "instance", "(.*).rcac.purdue.edu:9796"
        ),
         "metric", "Memory", "node", "(.+)"
      )
    |||,
    legendFormat='{{ node }}'
  ),
]).addThresholds(
  [
    { color: 'green', value: 0.0},
    { color: 'yellow', value: 60},
    { color: 'orange', value: 80 },
    { color: 'red', value: 90 },
    ]
);

dashboard.new(
  'JupyterHub Dashboard',
  tags=['jupyterhub'],
  uid='hub-dashboard',
  editable=true,
  refresh='5s',
).addTemplates(
  templates
).addPanel(
  row.new('Analysis Facility stats'), {}
)
// .addPanel(currentRunningAFpodsSingle, gridPos={w: 8, h: 4})
.addPanel(currentRunningAFpods,       gridPos={w: 8, h: 8})
.addPanel(nodeCpuUtilGauge,           gridPos={w: 8, h: 4})
.addPanel(nodeMemUtilGauge,           gridPos={w: 8, h: 4})
.addPanel(usersPerNode,               gridPos={w: 8, h: 8})
.addPanel(nodeCpuUtil,                gridPos={w: 8, h: 4})
.addPanel(nodeMemoryUtil,             gridPos={w: 8, h: 4})
.addPanel(podAgeDistribution,         gridPos={w: 8, h: 8})
.addPanel(hubResponseCodesDev,        gridPos={w: 8, h: 8})
.addPanel(hubResponseLatencyDev,      gridPos={w: 8, h: 8})
.addPanel(serverStartTimesDev,        gridPos={w: 8, h: 8})
