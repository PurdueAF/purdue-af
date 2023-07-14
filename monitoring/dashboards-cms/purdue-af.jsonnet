#!/usr/bin/env jsonnet -J ../vendor
// Deploys one dashboard with useful stats about usage & diagnostics.
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


local totalRunningPods = singlestat.new(
  '',
  colorValue=true,
  valueName='last',
  datasource='$PROMETHEUS_DS',
).addTarget(
  prometheus.target(
    |||
      sum(count by (namespace)(kube_pod_labels{pod=~"purdue-af-.*", namespace=~"cms(-dev)?"}))
    |||,
    legendFormat="Running AF pods"
  )
);
local totalRegisteredUsers = singlestat.new(
  '',
  colorValue=true,
  valueName='last',
  datasource='$PROMETHEUS_DS',
).addTarget(
  prometheus.target(
    |||
      sum(jupyterhub_total_users{job="jupyterhub"})
    |||,
    legendFormat="Total registered users"
  )
);



local usersPerNamespace = graphPanel.new(
  'Current users per namespace',
  decimals=0,
  fillGradient=3,
  min=0,
  // transparent=true,
  legend_rightSide=true,
  datasource='$PROMETHEUS_DS',
).addTargets([
  prometheus.target(
    |||
      count by (namespace)(kube_pod_labels{pod=~"purdue-af-.*", namespace=~"cms(-dev)?"})
    |||,
    legendFormat='{{namespace}}',
  ),
]);

local usersPerNode = graphPanel.new(
  'Current users per node',
  decimals=0,
  min=0,
  legend_rightSide=true,
  fillGradient=3,
  datasource='$PROMETHEUS_DS'
).addTargets([
  prometheus.target(
    |||
      count by (node)(kube_pod_info{namespace=~"cms(-dev)?", node!="", pod=~"purdue-af-.*"})
    |||,
    legendFormat='{{ node }}'
  ),
]);

local nodeCpuUtil = graphPanel.new(
  '',
  formatY1='percentunit',
  description=|||
    % of available CPUs currently in use
  |||,
  min=0,
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
  formatY1='percentunit',
  description=|||
    % of available Memory currently in use
  |||,
  min=0,
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

local daskSlurmSchedulers = graphPanel.new(
  'Number of active Dask SLURM schedulers',
  decimals=0,
  stack=false,
  min=0,
  datasource='$PROMETHEUS_DS'
).addTargets([
  prometheus.target(
    |||
      count(dask_scheduler_workers)/4 or vector(0)
    |||,
    legendFormat='Number of schedulers',
  ),
]);

local daskSlurmWorkers = graphPanel.new(
  'Number of Dask SLURM workers created on Hammer',
  decimals=0,
  stack=false,
  min=0,
  datasource='$PROMETHEUS_DS'
).addTargets([
  prometheus.target(
    |||
      sum(dask_scheduler_workers) or vector(0)
    |||,
    legendFormat='Number of workers',
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
  reducerFunction='last',
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
  reducerFunction='last',
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

local podAgeDistribution = heatmapPanel.new(
  'Age distribution of running AF pods',
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

// Hub usage stats
local tritonInferenceCount = graphPanel.new(
  'Inferences per second (standalone Triton servers)',
  decimals=0,
  stack=false,
  min=0,
  datasource='$PROMETHEUS_DS',
  legend_rightSide=true,
).addTargets([
  prometheus.target(
    |||
      rate(
            (
                sum(nv_inference_count) by (model)
            )[1m:1s]
        )
    |||,
    legendFormat='{{model}}',
  ),
]);

local hubResponseLatency = graphPanel.new(
  'Hub response latency',
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
              instance="cms.geddes.rcac.purdue.edu:80",
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
              instance="cms.geddes.rcac.purdue.edu:80",
              # Ignore SpawnProgressAPIHandler, as it is a EventSource stream
              # and keeps long lived connections open
              handler!="jupyterhub.apihandlers.users.SpawnProgressAPIHandler"
            }[5m]
          )
        ) by (le))
    |||,
    legendFormat='50th percentile'
  ),
  // prometheus.target(
  //   |||
  //     histogram_quantile(
  //       0.25,
  //       sum(
  //         rate(
  //           jupyterhub_request_duration_seconds_bucket{
  //             job="jupyterhub",
  //             instance="cms.geddes.rcac.purdue.edu:80",
  //             # Ignore SpawnProgressAPIHandler, as it is a EventSource stream
  //             # and keeps long lived connections open
  //             handler!="jupyterhub.apihandlers.users.SpawnProgressAPIHandler"
  //           }[5m]
  //         )
  //       ) by (le))
  //   |||,
  //   legendFormat='25th percentile'
  // ),
]);


local hubResponseCodes = graphPanel.new(
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
            instance="cms.geddes.rcac.purdue.edu:80",
          }[2m]
        )
      ) by (code)
    |||,
    legendFormat='{{ code }}'
  ),
]);

local serverStartTimes = graphPanel.new(
  'Server Start Times',
  formatY1='s',
  lines=false,
  min=0,
  points=true,
  pointradius=2,
  datasource='$PROMETHEUS_DS'
).addTargets([
  prometheus.target(
    'histogram_quantile(0.99, sum(rate(jupyterhub_server_spawn_duration_seconds_bucket{
      job="jupyterhub",
      instance="cms.geddes.rcac.purdue.edu:80"
    }[5m])) by (le))',
    legendFormat='99th percentile'
  ),
  prometheus.target(
    'histogram_quantile(0.5, sum(rate(jupyterhub_server_spawn_duration_seconds_bucket{
      job="jupyterhub",
      instance="cms.geddes.rcac.purdue.edu:80",
    }[5m])) by (le))',
    legendFormat='50th percentile'
  ),
]);

local placeholder = graphPanel.new('');
local placeholder_tr = graphPanel.new('',transparent=true);

dashboard.new(
  'Analysis Facility Dashboard',
  tags=['analysis-facility'],
  uid='purdue-af-dashboard',
  editable=true
).addTemplates(
  templates
)
.addPanel(row.new('Analysis Facility metrics'), {})
.addPanel(totalRunningPods,           gridPos={w: 4, h: 3})
.addPanel(usersPerNamespace,          gridPos={w: 8, h: 6})
.addPanel(nodeCpuUtilGauge,           gridPos={w: 12, h: 5})

.addPanel(totalRegisteredUsers,       gridPos={w: 4, h: 3})
.addPanel(placeholder_tr,             gridPos={w: 8, h: 0})
.addPanel(nodeCpuUtil,                gridPos={w: 12, h: 6})

.addPanel(usersPerNode,               gridPos={w: 12, h: 8})

.addPanel(placeholder,                gridPos={w: 12, h: 0})
.addPanel(placeholder_tr,             gridPos={w: 12, h: 0})
.addPanel(nodeMemUtilGauge,           gridPos={w: 12, h: 5})

.addPanel(placeholder_tr,                gridPos={w: 12, h: 0})
.addPanel(nodeMemoryUtil,             gridPos={w: 12, h: 6})

// .addPanel(podAgeDistribution,         gridPos={w: 8, h: 8})
.addPanel(row.new('Dask stats'), {})
.addPanel(daskSlurmSchedulers,        gridPos={w: 12, h: 10})
.addPanel(daskSlurmWorkers,           gridPos={w: 12, h: 10})
.addPanel(row.new('Triton stats'), {})
.addPanel(tritonInferenceCount,       gridPos={w: 18, h: 12})
.addPanel(row.new('JupyterHub diagnostics'), {})
.addPanel(hubResponseCodes,           gridPos={w: 8, h: 10})
.addPanel(hubResponseLatency,         gridPos={w: 8, h: 10})
.addPanel(serverStartTimes,           gridPos={w: 8, h: 10})
