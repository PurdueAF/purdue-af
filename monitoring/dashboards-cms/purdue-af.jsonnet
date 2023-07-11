#!/usr/bin/env jsonnet -J ../vendor
// Deploys one dashboard with useful stats about usage & diagnostics.
local grafana = import 'grafonnet/grafana.libsonnet';
local dashboard = grafana.dashboard;
local singlestat = grafana.singlestat;
local graphPanel = grafana.graphPanel;
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
  template.new(
    'hub',
    datasource='$PROMETHEUS_DS',
    query='label_values(kube_service_labels{service="hub"}, namespace)',
    // Allow viewing dashboard for multiple combined hubs
    includeAll=true,
    multi=true
  ),
];


// Hub usage stats
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
          kube_pod_created{pod=~"purdue-af-.*", namespace=~"cms"}
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
  datasource='$PROMETHEUS_DS'
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
  prometheus.target(
    |||
      histogram_quantile(
        0.25,
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
    legendFormat='25th percentile'
  ),
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
    // Metrics from hub seems to have `namespace` rather than just `namespace`
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


dashboard.new(
  'Purdue Analysis Facility Dashboard',
  tags=['analysis-facility'],
  uid='purdue-af-dashboard',
  editable=true
).addTemplates(
  templates
).addPanel(
  row.new('Purdue Analysis Facility metrics'), {}
).addPanel(
  currentRunningAFpods, {}
).addPanel(
  usersPerNode, {}
).addPanel(
  podAgeDistribution, {}
).addPanel(
  tritonInferenceCount, {}
).addPanel(
  hubResponseLatency, {}
).addPanel(
  hubResponseCodes, {}
).addPanel(
  serverStartTimes, {}
)
