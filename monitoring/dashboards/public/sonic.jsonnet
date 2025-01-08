local g = import 'github.com/grafana/grafonnet/gen/grafonnet-latest/main.libsonnet';
local prometheus = import 'prometheus.libsonnet';
local panels = import 'panels.libsonnet';

local myPanels = {
  row: import 'row.jsonnet',
  timeSeries: import 'timeSeries.jsonnet',
  stat: import 'stat.jsonnet',
  table: import 'table.jsonnet',
  gauge: import 'gauge.jsonnet',
  barGauge: import 'barGauge.jsonnet',
  heatmap: import 'heatmap.jsonnet',
  placeholder: import 'placeholder.jsonnet',
  text: import 'text.jsonnet',
  stateTimeline: import 'stateTimeline.jsonnet',
};

local w = g.panel.timeSeries.gridPos.withW;
local h = g.panel.timeSeries.gridPos.withH;

local placeholder = g.panel.canvas.new('')+g.panel.text.panelOptions.withTransparent();


local instance =
  g.dashboard.variable.query.new(
    'instance',
  )
  + g.dashboard.variable.query.queryTypes.withLabelValues(
    'instance',
    metric='envoy_cluster_membership_total{component="envoy"}',
  )
  + g.dashboard.variable.query.withDatasource(
    type='prometheus',
    uid='prometheus',
  )
  + g.dashboard.variable.query.withSort(1)
  + g.dashboard.variable.query.selectionOptions.withIncludeAll()
  + g.dashboard.variable.query.selectionOptions.withMulti()
;



local sonicLogo = panels.text(
    // title='',
    // description='',
    content=|||
      <div style="display: flex; flex-direction: column; align-items: center; width: 100%; height: 100%; box-sizing: border-box;">
          <img src="https://yongbinfeng.gitbook.io/~gitbook/image?url=https%3A%2F%2F2104107444-files.gitbook.io%2F%7E%2Ffiles%2Fv0%2Fb%2Fgitbook-x-prod.appspot.com%2Fo%2Fspaces%252Fgj0XlXp8qhEh0Cusn3gq%252Fuploads%252Fq0BdjaLNbsosFn9OK8N0%252FSONIC_Logo.png%3Falt%3Dmedia%26token%3D8cdf9ae5-d8e1-46ec-a532-a2493e65e377&width=300&dpr=4&quality=100&sign=ecb659d174c2a67811253fce3b00b25903ab7a2198911dc7f2407a12976319dc" alt="SONIC Logo" style="max-width: 100%; max-height: calc(50% - 20px); height: auto; padding-top: 10px; padding-bottom: 10px;">
          <img src="https://tikz.net/wp-content/uploads/2023/09/CMS_logo-008.png" style="max-width: 100%; max-height: calc(50% - 20px); height: auto; padding-top: 10px; padding-bottom: 10px">
      </div>
    |||,

    transparent=true,
    mode='html'
);

local sonicTitle = panels.text(
    // title='',
    // description='',
    content=|||
        # Purdue SONIC servers

        This dashboard provides metrics for SONIC load balancers deployed at the
        Purdue CMS Tier-2 computing site.

        Each load balancer contains the following components:

        - Kubernetes Deployment with one or multiple Nvidia Triton servers, each containing an Nvidia T4 GPU
        - Routing table (headless Kubernetes Service) with IPs of the Triton servers
        - Envoy Proxy for routing algorithms (load balancing and fallback)
        - External endpoint for client connections
    |||,
    transparent=true
);

local deployedTritonLB = panels.stat(
  targets=[
    prometheus.addQuery(
      'prometheus',
      'count(sum by (instance) (nv_inference_count{instance=~"$instance"}))',
      legendFormat='Active SONIC instances',
      instant=true
    ),
  ],
  colorMode='value'
);

local deployedTritonServers = panels.stat(
  targets=[
    prometheus.addQuery(
      'prometheus',
      |||
        sum (
          envoy_cluster_membership_total{envoy_cluster_name="triton_grpc_service", component="envoy", instance=~"$instance"}
        )
      |||,
      legendFormat='Active Triton servers',
      instant=true
    ),
  ],
  colorMode='value'
);

local sonicStatus = panels.stateTimeline(
  title='Load balancer saturation status',
  targets=[
    prometheus.addQuery(
      'prometheus',
      |||
        sum(
          rate(
            envoy_http_downstream_rq_time_sum{envoy_http_conn_manager_prefix="ingress_grpc", instance=~"$instance"}
          [5m:1m])
          /
          rate(
            envoy_http_downstream_rq_time_count{envoy_http_conn_manager_prefix="ingress_grpc", instance=~"$instance"}
          [5m:1m])
        ) by (instance)
      |||,
      legendFormat='{{ instance }}'
    ),
  ],
  showValue='never',
  unit='ms',
  thresholdMode='absolute',
  thresholdSteps=[
    { color: 'green', value: 0 },
    { color: 'yellow', value: 50 },
    { color: 'orange', value: 100 },
    { color: 'red', value: 200 },
  ],
);

local latencyByModel = panels.timeSeries(
  title='Inference latency by model',
  targets=[
    prometheus.addQuery(
      'prometheus-rancher',
      |||
        sum by (model, version) (rate(nv_inference_compute_infer_duration_us{pod=~"$instance.*"}[5m:1m])) / 1000 / sum by (model, version) (rate(nv_inference_exec_count[5m:1m]))
      |||,
      legendFormat='{{ model }} v{{ version }}'
    ),
  ],
  unit='ms',
  min=0,
  legendPlacement='right',
);

local tritonInferencesPerLB = panels.timeSeries(
  title='Inference rate (batches per second)',
  targets=[
    prometheus.addQuery(
      'prometheus-rancher',
      |||
        sum(
          rate(
            nv_inference_exec_count{pod=~"$instance.*"}[5m:1m]
          )
        ) by (service)
      |||,
      legendFormat='{{ app }}'
    ),
  ],
  min=0,
  legendPlacement='right',
);

local tritonInferencesEvPerLB = panels.timeSeries(
  title='Inference rate (events per second)',
  targets=[
    prometheus.addQuery(
      'prometheus-rancher',
      |||
        sum (
          rate(
            nv_inference_count{pod=~"$instance.*"}[5m:1m]
          )
        ) by (service)
      |||,
      legendFormat='{{ app }}'
    ),
  ],
  min=0,
  legendPlacement='right',
);

local tritonInferencesPerModel = panels.timeSeries(
  title='Inference rate (batches per second)',
  targets=[
    prometheus.addQuery(
      'prometheus-rancher',
      |||
        sum (
          rate(
            nv_inference_exec_count{pod=~"$instance.*"}[5m:1m]
          )
        ) by (model, version)
      |||,
      legendFormat='{{ model }} v{{ version }}'
    ),
  ],
  min=0,
  legendPlacement='right',
);

local tritonInferencesEvPerModel = panels.timeSeries(
  title='Inference rate (events per second)',
  targets=[
    prometheus.addQuery(
      'prometheus-rancher',
      |||
        sum (
          rate(
            nv_inference_count{pod=~"$instance.*"}[5m:1m]
          )
        ) by (model, version)
      |||,
      legendFormat='{{ model }} v{{ version }}'
    ),
  ],
  min=0,
  legendPlacement='right',
);

local tritonNumServers = panels.timeSeries(
  title='Triton servers per SONIC instance',
  targets=[
    prometheus.addQuery(
      'prometheus',
      |||
        envoy_cluster_membership_total{envoy_cluster_name="triton_grpc_service", component="envoy"}
      |||,
      legendFormat='{{ instance }}'
    ),
  ],
  min=0,
  legendPlacement='right',
);


local tritonQueueTimeByModel = panels.timeSeries(
  title='Queue time by model, avg. over Triton servers [ms]',
  description='',
  targets=[
    prometheus.addQuery(
      'prometheus-rancher',
      |||
        sum by (model, version) (
          avg by (model, pod, version) (
            irate(nv_inference_queue_duration_us{pod=~"$instance.*"}[5m])
            /
            (1000 * (1 + 
              irate(nv_inference_request_success{pod=~"$instance.*"}[5m])
            ))
          )
        )
      |||,
      legendFormat='{{ model }} v{{ version }}'
    ),
  ],
  min=0,
  legendPlacement='right',
);

local totalLatency = panels.timeSeries(
  title='Server load metric',
  description='',
  targets=[
    prometheus.addQuery(
      'prometheus',
      |||
        sum by (instance) (
          rate(envoy_http_downstream_rq_time_sum{envoy_http_conn_manager_prefix="ingress_grpc", instance=~"$instance"}[5m:1m])
          /
          rate(envoy_http_downstream_rq_time_count{envoy_http_conn_manager_prefix="ingress_grpc", instance=~"$instance"}[5m:1m])
        )
      |||,
      legendFormat='{{ instance }}'
    ),
  ],
  min=0,
  legendPlacement='right',
  unit='ms',
  thresholdMode='dashed',
  thresholdSteps=[
    { color: 'green', value: 0 },
    { color: 'orange', value: 0.00001 },
    { color: 'red', value: 100 },
  ],
  fillOpacity=20,
);

local tritonGPUload = panels.timeSeries(
  title='GPU utilization per Triton server',
  description='',
  targets=[
    prometheus.addQuery(
      'prometheus-rancher',
      |||
        avg_over_time(nv_gpu_utilization{pod=~"$instance.*"}[5m:1m])
      |||,
      legendFormat='{{ pod }}'
    ),
  ],
  min=0,
  unit='percentunit',
  legendPlacement='right',
);

local gpuGrEngineUtil = panels.timeSeries(
  title='GPU Graphics Engine Utilization',
  targets=[
    prometheus.addQuery(
      'prometheus-rancher',
      'sum by (gpu, GPU_I_ID, GPU_I_PROFILE, kubernetes_node) (DCGM_FI_PROF_GR_ENGINE_ACTIVE{kubernetes_node=~"paf-.*"})',
      legendFormat='{{kubernetes_node}}-{{gpu}}'
    ),
  ],
  unit='percentunit',
  min=0,
  legendPlacement='right',
);

local envoyLatency = panels.timeSeries(
  title='Envoy Proxy latency [ms]',
  description='',
  targets=[
    prometheus.addQuery(
      'prometheus',
      |||
        sum(
          rate(envoy_http_downstream_rq_time_sum{envoy_http_conn_manager_prefix="ingress_grpc"}[5m:1m])
          /
          rate(envoy_http_downstream_rq_time_count{envoy_http_conn_manager_prefix="ingress_grpc"}[5m:1m])
        ) by (instance)
      |||,
      legendFormat='{{ instance }}'
    ),
  ],
  min=0,
  unit='ms',
  legendPlacement='right',
);

local sonicLatency = panels.timeSeries(
  title='SONIC latency breakdown',
  description='',
  targets=[
    prometheus.addQuery(
      'prometheus',
      |||
        sum (
          sum by (instance)(
            rate(nv_inference_compute_infer_duration_us{instance=~"$instance"}[5m:1m])
          )
        /
          sum by (instance) (((
            rate(nv_inference_exec_count{instance=~"$instance"}[5m:1m])
          +0.000000000000001)*1000))
        )
      |||,
      legendFormat='Inference'
    ),
    prometheus.addQuery(
      'prometheus',
      |||
        sum(
          sum by (instance)(
            rate(nv_inference_queue_duration_us{instance=~"$instance"}[5m:1m])
          )
        /
          sum by (instance) (((
            rate(nv_inference_exec_count{instance=~"$instance"}[5m:1m])
          +0.000000000000001)*1000))
        )
      |||,
      legendFormat='Queue'
    ),
    prometheus.addQuery(
      'prometheus',
      |||
        sum(
          sum by (instance)(
            rate(nv_inference_compute_input_duration_us{instance=~"$instance"}[5m:1m])
          )
        /
          sum by (instance) (((
            rate(nv_inference_exec_count{instance=~"$instance"}[5m:1m])
          +0.000000000000001)*1000))
        )
      |||,
      legendFormat='Input'
    ),
    prometheus.addQuery(
      'prometheus',
      |||
        sum(
          sum by (instance)(
            rate(nv_inference_compute_output_duration_us{instance=~"$instance"}[5m:1m])
          )
        /
          sum by (instance) (((
            rate(nv_inference_exec_count{instance=~"$instance"}[5m:1m])
          +0.000000000000001)*1000))
        )
      |||,
      legendFormat='Output'
    ),
    prometheus.addQuery(
      'prometheus',
      |||
        sum(
          sum(
            rate(envoy_http_downstream_rq_time_sum{envoy_http_conn_manager_prefix="ingress_grpc", instance=~"$instance"}[5m:1m])
            /
            rate(envoy_http_downstream_rq_time_count{envoy_http_conn_manager_prefix="ingress_grpc", instance=~"$instance"}[5m:1m])
          ) by (instance)
        -
          sum by (instance)(
            rate(nv_inference_request_duration_us{instance=~"$instance"}[5m:1m])
          )
        /
          sum by (instance) (((
            rate(nv_inference_exec_count{instance=~"$instance"}[5m:1m])
          +0.000000000000001)*1000))
        )
      |||,
      legendFormat='Other sources'
    ),
  ],
  min=0,
  unit='ms',
  legendPlacement='right',
  stackingMode='normal',
  gradientMode='opacity',
  fillOpacity=100,
  // logBase=2
);

local envoyOverhead = panels.timeSeries(
  title='Envoy overhead [ms]',
  description='',
  targets=[
    prometheus.addQuery(
      'prometheus',
      |||
        sum(
          rate(envoy_http_downstream_rq_time_sum{envoy_http_conn_manager_prefix="ingress_grpc"}[5m:1m])
          /
          rate(envoy_http_downstream_rq_time_count{envoy_http_conn_manager_prefix="ingress_grpc"}[5m:1m])
        ) by (instance)
        -           
        sum by (instance)(
          rate(nv_inference_request_duration_us[5m:1m])
        )
        /
        sum by (instance) (((
          rate(nv_inference_exec_count[5m:1m])
        +0.000000000000001)*1000))
      |||,
      legendFormat='{{ instance }}'
    ),
  ],
  min=0,
  unit='ms',
  legendPlacement='right',
);

local envoyClients = panels.timeSeries(
  title='Active connections to Envoy Proxy',
  description='',
  targets=[
    prometheus.addQuery(
      'prometheus',
      |||
        sum by (instance) (
          envoy_http_downstream_cx_active{envoy_http_conn_manager_prefix="ingress_grpc", instance=~"$instance"}
        )
      |||,
      legendFormat='{{ instance }}'
    ),
  ],
  min=0,
  legendPlacement='bottom',
);

local envoyConnRate = panels.timeSeries(
  title='Rate of new connections',
  description='',
  targets=[
    prometheus.addQuery(
      'prometheus',
      |||
        sum by (instance) (
          rate(envoy_http_downstream_cx_active{envoy_http_conn_manager_prefix="ingress_grpc", instance=~"$instance"}[2m:30s])
        )
      |||,
      legendFormat='{{ instance }}'
    ),
  ],
  min=0,
  legendPlacement='bottom',
);

local envoyMemUtil = panels.timeSeries(
  title='Envoy Proxy memory utilization',
  description='',
  targets=[
    prometheus.addQuery(
      'prometheus-rancher',
      |||
        sum by (pod)(
            container_memory_working_set_bytes{namespace="cms", container=~"envoy"}
        ) /
        sum by (pod)(
            kube_pod_container_resource_requests{namespace="cms", container=~"envoy", resource="memory"}
        )
      |||,
      legendFormat='{{ pod }}'
    ),
  ],
  unit='percentunit',
  min=0,
  // max=1,
  legendPlacement='bottom',
);

local envoyReqRate = panels.timeSeries(
  title='Envoy Proxy request rates',
  description='',
  targets=[
    prometheus.addQuery(
      'prometheus',
      |||
        sum(sum by (instance) (
          rate(envoy_http_rq_total[5m:1m])
        ))
      |||,
      legendFormat='{{ instance }}'
    ),
  ],
  // unit='percentunit',
  min=0,
  legendPlacement='bottom',
  thresholdMode='area',
  thresholdSteps=[
    { color: 'green', value: 0 },
    { color: 'red', value: 900 },
  ]
);

local envoyCpuUtil = panels.timeSeries(
  title='Envoy Proxy CPU utilization',
  description='',
  targets=[
    prometheus.addQuery(
      'prometheus-rancher',
      |||
        sum(rate(
          container_cpu_usage_seconds_total{namespace="cms", container="envoy"}[5m:1m]
        )) by (pod)
      |||,
      legendFormat='{{ pod }}'
    ),
  ],
  unit='cpu',
  min=0,
  max=9,
  legendPlacement='bottom',
  thresholdMode='dashed',
  thresholdSteps=[
    { color: 'green', value: 0 },
    { color: 'red', value: 8 },
  ]
);

local tritonMemUtil = panels.timeSeries(
  title='Memory utilization by Triton pods',
  description='',
  targets=[
    prometheus.addQuery(
      'prometheus-rancher',
      |||
        sum by (pod)(
            container_memory_working_set_bytes{namespace="cms", container=~"$instance.*"}
        ) /
        sum by (pod)(
            kube_pod_container_resource_requests{namespace="cms", container=~"$instance.*", resource="memory"}
        )
      |||,
      legendFormat='{{ pod }}'
    ),
  ],
  unit='percentunit',
  min=0, max=1,
  legendPlacement='right',
);


local gpuPowerUsage = panels.timeSeries(
  title='GPU power usage (all GPUs in a load balancer)',
  targets=[
    prometheus.addQuery(
      'prometheus-rancher',
      |||
        sum(nv_gpu_power_usage) by (pod)
      |||,
      legendFormat='{{ pod }}'
    ),
  ],
  min=0,
  legendPlacement='right',
);

local inferencesPerSecondPerWatt = panels.timeSeries(
  title='Inferences per second per Watt',
  targets=[
    prometheus.addQuery(
      'prometheus-rancher',
      |||
        sum by (pod)(
          rate(nv_inference_count{pod=~"$instance.*"}[5m:1m])
        )
        /
        sum by (pod)(
          nv_gpu_power_usage{pod=~"$instance.*"}
        )
      |||,
      legendFormat='{{ pod }}'
    ),
  ],
  min=0,
  legendPlacement='right',
);

local networkTraffic = panels.timeSeries(
  title='Total Network Traffic',
  description='',
  targets=[
    prometheus.addQuery(
      'prometheus',
      |||
        sum by (instance) (
          (
            rate(
                envoy_http_downstream_cx_rx_bytes_total{envoy_http_conn_manager_prefix="ingress_grpc", instance=~"$instance"}[5m:1m]
            ) + rate(
                envoy_http_downstream_cx_tx_bytes_total{envoy_http_conn_manager_prefix="ingress_grpc", instance=~"$instance"}[5m:1m]
            )
          ) / (1024 * 1024 * 1024) * 8
        )
      |||,
      legendFormat='{{ instance }}',
    ),
  ],
  min=0,
  legendPlacement='right',
  unit='Gbps',
);


local pingGraph = g.panel.geomap.new('Network Latency from Purdue') +
  g.query.prometheus.withDatasource('prometheus') +
  g.panel.geomap.queryOptions.withTargets(
    [
      prometheus.addQuery(
        'prometheus',
        |||
          ping_latency_ms
        |||,
        legendFormat='{{ name }}',
        // refId='nodes',
        instant=true,
        format='table'
      ),
    ]
  )
  + g.panel.geomap.queryOptions.withTransformations([
      g.panel.geomap.queryOptions.transformation.withId('filterFieldsByName')
    + g.panel.geomap.queryOptions.transformation.withOptions(
        {
            "include": {
              "names": [
                "ip",
                "name",
                "Value",
                "latitude",
                "longitude"
              ]
            },

        }
    ),
    g.panel.geomap.transformation.withId('organize')
    + g.panel.geomap.transformation.withOptions(
        {
            "indexByName": {
                "name": 0,
                "ip": 1,
                "Value": 2
            },
        }
    ),
  ])
  + g.panel.geomap.options.withView(
    {
      "allLayers": true,
      "id": "coords",
      "lat": 35,
      "lon": -97,
      "zoom": 3.6
    }
  ) +
  g.panel.geomap.options.withLayers(
    [
      {
        "type": "markers",
        "name": "Ping from Purdue",
        "config": {
          "style": {
            "size": {
              "min": 1,
              "max": 10,
              "field": "Value"
            },
            "color": {
              "field": "Value"
            },
            "opacity": 0.4,
            "symbol": {
              "mode": "fixed",
              "fixed": "img/icons/marker/circle.svg"
            },
            "symbolAlign": {
              "horizontal": "center",
              "vertical": "center"
            },
            "text": {
              "fixed": "",
              "mode": "field",
              "field": "Value"
            },
            "textConfig": {
              "textAlign": "right",
              "textBaseline": "bottom"
            }
          },
          "showLegend": true
        },
        "location": {
          "mode": "auto"
        },
        "tooltip": true
      }
    ],
  )
  + g.panel.geomap.standardOptions.thresholds.withSteps(
    [
      { color: 'green', value: 0 },
      { color: 'yellow', value: 1 },
      { color: 'orange', value: 10 },
      { color: 'red', value: 100 },
    ],
  )
  + g.panel.geomap.panelOptions.withTransparent()
  + g.panel.geomap.standardOptions.withOverrides([
    g.panel.geomap.fieldOverride.byName.new("Value")
    + g.panel.geomap.fieldOverride.byName.withPropertiesFromOptions(
      g.panel.geomap.standardOptions.withUnit('ms')
      + g.panel.table.standardOptions.withDisplayName('Latency')
    )
  ])
;

g.dashboard.new('SONIC Dashboard')
+ g.dashboard.withUid('sonic-dashboard')
+ g.dashboard.withDescription('SONIC monitoring')
// + g.dashboard.withLiveNow()
+ g.dashboard.withRefresh('1m')
// + g.dashboard.withStyle(value="dark")
+ g.dashboard.withTimezone(value="browser")
+ g.dashboard.time.withFrom(value="now-6h")
+ g.dashboard.time.withTo(value="now")
+ g.dashboard.graphTooltip.withSharedCrosshair()
+ g.dashboard.withVariables([
  instance,
])
+ g.dashboard.withPanels([

  sonicLogo              + w(2)   + h(7),
  sonicTitle             + w(14)  + h(7),
  pingGraph              + w(8) + h(11),
  sonicStatus            + w(16)  + h(4),
  placeholder            + w(8)  + h(0),

  sonicLatency           + w(9) + h(11),
  totalLatency           + w(9) + h(11),
  deployedTritonLB       + w(6)   + h(2),
  placeholder            + w(18)  + h(0),
  deployedTritonServers  + w(6)   + h(2),
  placeholder            + w(18)  + h(0),
  tritonNumServers       + w(6)   + h(7),


  latencyByModel           + w(12) + h(10),
  tritonQueueTimeByModel   + w(12) + h(10),

  tritonGPUload       + w(12) + h(10),
  gpuGrEngineUtil     + w(12) + h(10),
  // envoyLatency  + w(8) + h(10),
  // envoyOverhead  + w(8) + h(10),
  envoyClients  + w(6) + h(8),
  envoyConnRate + w(6) + h(8),
  envoyMemUtil  + w(6) + h(8),
  envoyCpuUtil  + w(6) + h(8),
  tritonInferencesPerLB + w(12) + h(10),
  tritonInferencesPerModel + w(12) + h(10),

  tritonInferencesEvPerLB + w(12) + h(10),
  tritonInferencesEvPerModel + w(12) + h(10),

  gpuPowerUsage  + w(12) + h(10),
  inferencesPerSecondPerWatt + w(12) + h(10),

  networkTraffic + w(10) + h(12),

  // tritonMemUtil       + w(8) + h(10),
  // tritonInferencesPerLB  + w(8) + h(10),

])