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
  datasource='$PROMETHEUS_DS',
).addTarget(
  prometheus.target(
    |||
      sum(count by (namespace)(kube_pod_labels{pod=~"purdue-af-.*", namespace=~"cms(-dev)?"}))
    |||,
    legendFormat="Current AF users", instant=true
  )
);
local totalRegisteredUsers = singlestat.new(
  '',
  colorValue=true,
  datasource='$PROMETHEUS_DS',
).addTarget(
  prometheus.target(
    |||
      sum(jupyterhub_total_users{job="jupyterhub"})
    |||,
    legendFormat="Total registered users", instant=true
  )
);



local usersPerNamespace = graphPanel.new(
  'Current users per namespace',
  decimals=0,
  // fillGradient=3,
  fill=6,
  min=0,
  stack=true,
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
  // stack=true,
  legend_rightSide=true,
  // fill=6,
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
  fillGradient=1,
  datasource='$PROMETHEUS_DS',
  legend_rightSide=true,
  transparent=true
).addTargets([
  prometheus.target(
    |||
      sum by (node)(
        label_replace(
          label_replace(
            rate(node_cpu_seconds_total{mode!="idle"}[1m]),
            "node", "$1", "instance", "(.*).rcac.purdue.edu:9796"
          ),
          "node", "$1", "node", "(.*).cms"
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
  fillGradient=1,
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
).addTargets([
  prometheus.target(
    |||
      label_replace(
        sum by (node)(
          label_replace(
            label_replace(
              rate(node_cpu_seconds_total{mode!="idle"}[1m]),
              "node", "$1", "instance", "(.*).rcac.purdue.edu:9796"
            ),
            "node", "$1", "node", "(.*).cms"
          )
        )
        /
        sum(kube_node_status_capacity{resource="cpu"}) by (node),
        "metric", "CPU", "node", "(.+)"
      )
    |||,
    legendFormat='{{ node }}', instant=true
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
    legendFormat='{{ node }}', instant=true
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
  yBucketSize=604800,
  yAxis_decimals=0,
  // tooltip_showHistogram='true',
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


local podStorageUtil = heatmapPanel.new(
  '/home/ storage utilization for acrive users',
  // xBucketSize and interval must match to get correct values out of heatmaps
  xBucketSize='300s',
  yAxis_format='percentunit',
  yBucketSize=0.1,
  yAxis_min=0,
  yAxis_max=1,
  // tooltip_showHistogram='true',
  // color_colorScheme='interpolateViridis',
  datasource='$PROMETHEUS_DS'
).addTargets([
  prometheus.target(
    |||
      af_home_dir_util{job="af-pod-monitor"}
    |||,
    interval='300s',
    intervalFactor=1,
  ),
]);

local gpuTemp = gaugePanel.new(
  'GPU Temperature',
  datasource='$PROMETHEUS_DS',
  description="",
  min=0,
  max=100,
  showThresholdLabels=false,
  decimals=0,
  // thresholdsMode='percentage',
  unit='celsius',
  transparent=true,
).addTargets([
  prometheus.target(
    |||
      avg (DCGM_FI_DEV_GPU_TEMP) by (gpu)
    |||,
    legendFormat='GPU #{{ gpu }}', instant=true
  ),
]).addThresholds(
  [
    { color: 'blue', value: 0},
    { color: 'green', value: 30},
    { color: 'yellow', value: 70},
    { color: 'orange', value: 80 },
    { color: 'red', value: 85 },
    ]
);

local gpuPower = graphPanel.new(
  'GPU Power Usage (Watts)',
  description='',
  datasource='$PROMETHEUS_DS',
  legend_rightSide=true,
  legend_sort=true,
  transparent=true,
).addTargets([
  prometheus.target(
    |||
      avg by (gpu) (avg_over_time(DCGM_FI_DEV_POWER_USAGE[10m:10s]))
    |||,
    legendFormat='GPU #{{gpu}}'
  ),
]);

local gpuSlices = tablePanel.new(
  '',
  description='',
  transform='timeseries_to_rows',
  transparent=true,
  datasource='$PROMETHEUS_DS',
  styles=[
      {pattern: 'GPU_I_PROFILE', type: 'string', alias: 'GPU slice'},
      {pattern: 'Value', type: 'number', alias: 'Number of slices'},
  ],
).addTarget(
  prometheus.target(
    |||
      count (DCGM_FI_DEV_GPU_TEMP) by (GPU_I_PROFILE)
    |||,
    legendFormat='{{GPU_I_PROFILE}}', instant=true, format='table'
  ),
).hideColumn('Time');

local gpuGrEngineUtil = graphPanel.new(
  'GPU Graphics Engine Utilization',
  formatY1='percentunit',
  description='',
  min=0,
  // since this is actual measured utilization, it should not be able to exceed max=1
  // max=1,
  datasource='$PROMETHEUS_DS',
  legend_rightSide=true,
  legend_sort=true,
  // transparent=true,
  // decimals=4
).addTargets([
  prometheus.target(
    |||
      sum by (GPU_I_ID, GPU_I_PROFILE, gpu) (DCGM_FI_PROF_GR_ENGINE_ACTIVE)
    |||,
    legendFormat='Slice ID {{GPU_I_ID}}: GPU #{{gpu}}, {{GPU_I_PROFILE}}'
  ),
]);



local deployedTritonLB= singlestat.new(
  '',
  colorValue=true,
  datasource='$PROMETHEUS_DS',
).addTarget(
  prometheus.target(
    |||
      count(sum by (app) (nv_inference_count))
    |||,
    legendFormat="Deployed load balancers",
    instant=true
  )
);

local deployedTritonServers= singlestat.new(
  '',
  colorValue=true,
  datasource='$PROMETHEUS_DS',
).addTarget(
  prometheus.target(
    |||
      sum (
          kube_deployment_status_replicas_available{namespace="cms", deployment=~"triton-(.*)"}
      )
    |||,
    legendFormat="Deployed Triton servers",
    instant=true
  )
);

local tritonTable= tablePanel.new(
  '',
  transform='timeseries_to_rows',
  transparent=true,
  datasource='$PROMETHEUS_DS',
  styles=[
      {pattern: 'name', type: 'string', alias: 'Load balancer'},
      {pattern: 'Value #A', type: 'number', alias: '# servers'},
      {pattern: 'Value #B', type: 'number', alias: '# models'},
  ],
)
.addTargets([
  prometheus.target(
    |||
      label_replace(
      sum by (name)(label_replace(
          kube_deployment_status_replicas_available{namespace="cms", deployment=~"triton-(.*)"},
          "name", "$1", "deployment", "(.*)"
      )),
      "name", "$1-triton", "name", "(.*)-nginx"
      )
    |||,
    legendFormat="{{name}}", instant=true, format='table'
  ),
  prometheus.target(
    |||
      count by (name) (
        count by (model, name) (
          label_replace(nv_inference_count{job="af-pod-monitor"}, "name", "$1", "app", "(.*)")
        )
      )
    |||,
    legendFormat="{{name}}", instant=true, format='table'
    ),
]).hideColumn('Time');

local tritonNumServers = graphPanel.new(
  'Triton servers per load balancer',
  decimals=0,
  stack=false,
  min=0,
  legend_rightSide=true,
  datasource='$PROMETHEUS_DS'
).addTargets([
  prometheus.target(
    |||
      sum by (deployment)(
          kube_deployment_status_replicas_available{namespace="cms", deployment=~"triton-(.*)"}
      )
    |||,
    legendFormat='{{deployment}}', 
  ),
]);

local tritonInferenceCount = graphPanel.new(
  'Inferences per second (all Triton servers)',
  // decimals=0,
  stack=false,
  min=0,
  datasource='$PROMETHEUS_DS',
  legend_rightSide=true,
).addTargets([
  prometheus.target(
    |||
      rate(
            (
                sum(nv_inference_count{job="af-pod-monitor"}) by (model)
            )[1m:1s]
        )
    |||,
    legendFormat='{{model}}',
  ),
]);

local tritonInferencesPerLB = graphPanel.new(
  'Inferences per load balancer (all models)',
  // decimals=0,
  stack=false,
  min=0,
  datasource='$PROMETHEUS_DS',
  legend_rightSide=true,
).addTargets([
  prometheus.target(
    |||
        rate(
              (
                  sum(nv_inference_count{job="af-pod-monitor"}) by (app)
              )[1m:1s]
          )
    |||,
    legendFormat='{{app}}',
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
      instance=~"cms(dev)?.geddes.rcac.purdue.edu:80"
    }[5m])) by (le))',
    legendFormat='99th percentile'
  ),
  prometheus.target(
    'histogram_quantile(0.5, sum(rate(jupyterhub_server_spawn_duration_seconds_bucket{
      job="jupyterhub",
      instance=~"cms(dev)?.geddes.rcac.purdue.edu:80",
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
  editable=true,
  refresh='10s',
).addTemplates(
  templates
)
.addPanel(row.new('Analysis Facility metrics'), {})
.addPanel(totalRunningPods,           gridPos={w: 4, h: 3})
.addPanel(usersPerNamespace,          gridPos={w: 7, h: 6})
.addPanel(nodeCpuUtilGauge,           gridPos={w: 13, h: 5})

.addPanel(totalRegisteredUsers,       gridPos={w: 4, h: 3})
.addPanel(placeholder_tr,             gridPos={w: 7, h: 0})
.addPanel(nodeCpuUtil,                gridPos={w: 13, h: 5})

.addPanel(usersPerNode,               gridPos={w: 11, h: 7})

.addPanel(placeholder,                gridPos={w: 13, h: 0})
.addPanel(placeholder_tr,             gridPos={w: 11, h: 0})
.addPanel(nodeMemUtilGauge,           gridPos={w: 13, h: 5})

// .addPanel(placeholder_tr,                gridPos={w: 12, h: 0})
.addPanel(podAgeDistribution,         gridPos={w: 6, h: 7})
.addPanel(podStorageUtil,             gridPos={w: 5, h: 7})
.addPanel(nodeMemoryUtil,             gridPos={w: 13, h: 5})

.addPanel(row.new('GPU metrics'), {})
.addPanel(gpuGrEngineUtil,            gridPos={w: 10,h: 8})
.addPanel(gpuTemp,                    gridPos={w: 6, h: 4})
.addPanel(gpuPower,                   gridPos={w: 8, h: 8})
.addPanel(placeholder_tr,             gridPos={w: 10,h: 0})
.addPanel(gpuSlices,                  gridPos={w: 6, h: 4})

.addPanel(row.new('Triton metrics'), {})
.addPanel(deployedTritonLB,           gridPos={w: 4, h: 3})
.addPanel(deployedTritonServers,      gridPos={w: 4, h: 3})
// .addPanel(placeholder_tr,             gridPos={w: 4, h: 0})
.addPanel(tritonInferenceCount,       gridPos={w: 16, h: 8})
.addPanel(tritonTable,                gridPos={w: 8, h: 7})
.addPanel(tritonInferencesPerLB,       gridPos={w: 16, h: 8})
// .addPanel(placeholder_tr,             gridPos={w: 16, h: 0})
.addPanel(tritonNumServers,           gridPos={w: 8, h: 6})

.addPanel(row.new('Dask metrics'), {})
.addPanel(daskSlurmSchedulers,        gridPos={w: 12, h: 10})
.addPanel(daskSlurmWorkers,           gridPos={w: 12, h: 10})

.addPanel(row.new('JupyterHub diagnostics'), {})
.addPanel(hubResponseCodes,           gridPos={w: 8, h: 10})
.addPanel(hubResponseLatency,         gridPos={w: 8, h: 10})
.addPanel(serverStartTimes,           gridPos={w: 8, h: 10})
