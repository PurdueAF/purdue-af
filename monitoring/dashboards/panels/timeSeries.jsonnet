local g = import 'github.com/grafana/grafonnet/gen/grafonnet-latest/main.libsonnet';

{
  usersPerNamespace:: g.panel.timeSeries.new('Current users per namespace')
  + g.panel.timeSeries.queryOptions.withTargets([
    g.query.prometheus.new(
      'prometheus',
      'count by (namespace)(kube_pod_labels{pod=~"purdue-af-.*", namespace=~"cms(-dev)?"})',
    ) + g.query.prometheus.withLegendFormat('{{namespace}}'),
  ])
  + g.panel.timeSeries.standardOptions.withMin(0)
  + g.panel.timeSeries.standardOptions.withDecimals(0)
  + g.panel.timeSeries.options.legend.withPlacement('right')
  + g.panel.timeSeries.options.tooltip.withMode('multi')
  + g.panel.timeSeries.fieldConfig.defaults.custom.stacking.withMode('normal')
  + g.panel.timeSeries.fieldConfig.defaults.custom.withFillOpacity(60),

  usersPerNode:: g.panel.timeSeries.new('Current users per node')
  + g.panel.timeSeries.queryOptions.withTargets([
    g.query.prometheus.new(
      'prometheus',
      'count by (node)(kube_pod_info{namespace=~"cms(-dev)?", node!="", pod=~"purdue-af-.*"})',
    ) + g.query.prometheus.withLegendFormat('{{ node }}'),
  ])
  + g.panel.timeSeries.standardOptions.withMin(0)
  + g.panel.timeSeries.standardOptions.withDecimals(0)
  + g.panel.timeSeries.options.legend.withPlacement('right')
  + g.panel.timeSeries.options.tooltip.withMode('multi')
  // + g.panel.timeSeries.fieldConfig.defaults.custom.stacking.withMode('normal')
  + g.panel.timeSeries.fieldConfig.defaults.custom.withFillOpacity(20)
  + g.panel.timeSeries.fieldConfig.defaults.custom.withGradientMode('opacity'),

  nodeCpuUtil:: g.panel.timeSeries.new('')
  + g.panel.timeSeries.panelOptions.withDescription('% of available CPUs currently in use')
  // + g.panel.timeSeries.panelOptions.withTransparent()
  + g.panel.timeSeries.queryOptions.withTargets([
    g.query.prometheus.new(
      'prometheus',
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
    ) + g.query.prometheus.withLegendFormat('{{ node }}'),
  ])
  + g.panel.timeSeries.standardOptions.withUnit('percentunit')
  + g.panel.timeSeries.standardOptions.withMin(0)
  + g.panel.timeSeries.options.legend.withPlacement('right')
  + g.panel.timeSeries.options.tooltip.withMode('multi')
  + g.panel.timeSeries.fieldConfig.defaults.custom.withFillOpacity(6)
  + g.panel.timeSeries.fieldConfig.defaults.custom.withGradientMode('opacity'),

  nodeMemoryUtil:: g.panel.timeSeries.new('')
  + g.panel.timeSeries.panelOptions.withDescription('% of available memory currently in use')
  // + g.panel.timeSeries.panelOptions.withTransparent()
  + g.panel.timeSeries.queryOptions.withTargets([
    g.query.prometheus.new(
      'prometheus',
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
    ) + g.query.prometheus.withLegendFormat('{{ node }}'),
  ])
  + g.panel.timeSeries.standardOptions.withUnit('percentunit')
  + g.panel.timeSeries.standardOptions.withMin(0)
  + g.panel.timeSeries.options.legend.withPlacement('right')
  + g.panel.timeSeries.options.tooltip.withMode('multi')
  + g.panel.timeSeries.fieldConfig.defaults.custom.withFillOpacity(6)
  + g.panel.timeSeries.fieldConfig.defaults.custom.withGradientMode('opacity'),

  gpuGrEngineUtil:: g.panel.timeSeries.new('GPU Graphics Engine Utilization')
  + g.panel.timeSeries.panelOptions.withDescription('')
  // + g.panel.timeSeries.panelOptions.withTransparent()
  + g.panel.timeSeries.queryOptions.withTargets([
    g.query.prometheus.new(
      'prometheus-rancher',
      'sum by (gpu, GPU_I_ID, GPU_I_PROFILE) (DCGM_FI_PROF_GR_ENGINE_ACTIVE{kubernetes_node="geddes-g000"})',
    )
    + g.query.prometheus.withLegendFormat('Slice ID {{GPU_I_ID}}, GPU #{{gpu}}: {{GPU_I_PROFILE}}'),
  ])
  + g.panel.timeSeries.standardOptions.withUnit('percentunit')
  + g.panel.timeSeries.standardOptions.withMin(0)
  + g.panel.timeSeries.options.legend.withPlacement('right')
  // + g.panel.timeSeries.options.legend.withSortBy([{field: "gpu", desc:}])
  + g.panel.timeSeries.options.tooltip.withMode('multi')
  + g.panel.timeSeries.fieldConfig.defaults.custom.withFillOpacity(6)
  + g.panel.timeSeries.fieldConfig.defaults.custom.withGradientMode('opacity'),

  gpuMemUtil:: g.panel.timeSeries.new('GPU Memory Utilization')
  + g.panel.timeSeries.panelOptions.withDescription('')
  // + g.panel.timeSeries.panelOptions.withTransparent()
  + g.panel.timeSeries.queryOptions.withTargets([
    g.query.prometheus.new(
      'prometheus-rancher',
      'sum by (gpu, GPU_I_ID, GPU_I_PROFILE) (
        ( DCGM_FI_DEV_FB_USED{kubernetes_node="geddes-g000"} 
          / ( DCGM_FI_DEV_FB_USED{kubernetes_node="geddes-g000"} + DCGM_FI_DEV_FB_FREE{kubernetes_node="geddes-g000"} )
        )
      )',
    )
    + g.query.prometheus.withLegendFormat('Slice ID {{GPU_I_ID}}, GPU #{{gpu}}: {{GPU_I_PROFILE}}'),
  ])
  + g.panel.timeSeries.standardOptions.withUnit('percentunit')
  + g.panel.timeSeries.standardOptions.withMin(0)
  + g.panel.timeSeries.options.legend.withPlacement('right')
  // + g.panel.timeSeries.options.legend.withSortBy([{field: "gpu", desc:}])
  + g.panel.timeSeries.options.tooltip.withMode('multi')
  + g.panel.timeSeries.fieldConfig.defaults.custom.withFillOpacity(6)
  + g.panel.timeSeries.fieldConfig.defaults.custom.withGradientMode('opacity'),

  gpuPower:: g.panel.timeSeries.new('GPU Power Usage (Watts)')
  + g.panel.timeSeries.panelOptions.withDescription('')
  // + g.panel.timeSeries.panelOptions.withTransparent()
  + g.panel.timeSeries.queryOptions.withTargets([
    g.query.prometheus.new(
      'prometheus-rancher',
      'avg by (gpu) (avg_over_time(DCGM_FI_DEV_POWER_USAGE{kubernetes_node="geddes-g000"}[10m:10s]))',
    ) + g.query.prometheus.withLegendFormat('GPU #{{gpu}}'),
  ])

  + g.panel.timeSeries.options.legend.withPlacement('right')
  + g.panel.timeSeries.options.tooltip.withMode('multi')
  + g.panel.timeSeries.fieldConfig.defaults.custom.withFillOpacity(6)
  + g.panel.timeSeries.fieldConfig.defaults.custom.withGradientMode('opacity'),


  tritonInferenceCount:: g.panel.timeSeries.new('Inferences per second (all Triton servers)')
  + g.panel.timeSeries.panelOptions.withDescription('')
  + g.panel.timeSeries.queryOptions.withTargets([
    g.query.prometheus.new(
      'prometheus',
      |||
        rate(
              (
                  sum(nv_inference_count{job="af-pod-monitor"}) by (model)
              )[1m:1s]
          )
      |||,
    ) + g.query.prometheus.withLegendFormat('{{ model }}'),
  ])
  + g.panel.timeSeries.options.legend.withPlacement('right')
  + g.panel.timeSeries.options.tooltip.withMode('multi')
  + g.panel.timeSeries.standardOptions.withMin(0)
  + g.panel.timeSeries.fieldConfig.defaults.custom.withFillOpacity(6)
  + g.panel.timeSeries.fieldConfig.defaults.custom.withGradientMode('opacity'),

  tritonInferencesPerLB:: g.panel.timeSeries.new('Inferences per load balancer (all models)')
  + g.panel.timeSeries.panelOptions.withDescription('')
  + g.panel.timeSeries.queryOptions.withTargets([
    g.query.prometheus.new(
      'prometheus',
      |||
        rate(
              (
                  sum(nv_inference_count{job="af-pod-monitor"}) by (app)
              )[1m:1s]
          )
      |||,
    ) + g.query.prometheus.withLegendFormat('{{ app }}'),
  ])
  + g.panel.timeSeries.options.legend.withPlacement('right')
  + g.panel.timeSeries.options.tooltip.withMode('multi')
  + g.panel.timeSeries.standardOptions.withMin(0)
  + g.panel.timeSeries.fieldConfig.defaults.custom.withFillOpacity(6)
  + g.panel.timeSeries.fieldConfig.defaults.custom.withGradientMode('opacity'),

  tritonNumServers:: g.panel.timeSeries.new('Triton servers per load balancer')
  + g.panel.timeSeries.panelOptions.withDescription('')
  + g.panel.timeSeries.queryOptions.withTargets([
    g.query.prometheus.new(
      'prometheus',
      |||
        sum by (deployment)(
            kube_deployment_status_replicas_available{namespace="cms", deployment=~"triton(.*)", deployment!="triton-nginx"}
        )
      |||,
    ) + g.query.prometheus.withLegendFormat('{{ deployment }}'),
  ])
  + g.panel.timeSeries.options.legend.withPlacement('right')
  + g.panel.timeSeries.options.tooltip.withMode('multi')
  + g.panel.timeSeries.standardOptions.withMin(0)
  + g.panel.timeSeries.fieldConfig.defaults.custom.withFillOpacity(6)
  + g.panel.timeSeries.fieldConfig.defaults.custom.withGradientMode('opacity'),

  tritonLBmetric:: g.panel.timeSeries.new('Avg. queue duration')
  + g.panel.timeSeries.panelOptions.withDescription('(metric for autoscaling)')
  + g.panel.timeSeries.queryOptions.withTargets([
    g.query.prometheus.new(
      'prometheus',
      |||
        max(avg by (model) (delta(nv_inference_queue_duration_us{app="triton-triton"}[30s])/(1000 * (1 + delta(nv_inference_request_success{app="triton-triton"}[30s])))))
      |||,
    ) + g.query.prometheus.withLegendFormat('Max avg. by model queue wait'),
  ])
  // + g.panel.timeSeries.options.legend.withPlacement('right')
  + g.panel.timeSeries.options.tooltip.withMode('multi')
  + g.panel.timeSeries.standardOptions.withMin(0)
  + g.panel.timeSeries.fieldConfig.defaults.custom.withFillOpacity(6)
  + g.panel.timeSeries.fieldConfig.defaults.custom.withGradientMode('opacity'),

  daskSlurmSchedulers:: g.panel.timeSeries.new('Number of Dask SLURM workers created on Hammer')
  + g.panel.timeSeries.panelOptions.withDescription('')
  + g.panel.timeSeries.queryOptions.withTargets([
    g.query.prometheus.new(
      'prometheus', 'count(dask_scheduler_workers)/4 or vector(0)',
    ) + g.query.prometheus.withLegendFormat('Number of schedulers'),
  ])
  + g.panel.timeSeries.standardOptions.withDecimals(0)
  + g.panel.timeSeries.options.legend.withPlacement('right')
  + g.panel.timeSeries.options.tooltip.withMode('multi')
  + g.panel.timeSeries.standardOptions.withMin(0)
  + g.panel.timeSeries.fieldConfig.defaults.custom.withFillOpacity(6)
  + g.panel.timeSeries.fieldConfig.defaults.custom.withGradientMode('opacity'),

  daskSlurmWorkers:: g.panel.timeSeries.new('Number of active Dask SLURM schedulers')
  + g.panel.timeSeries.panelOptions.withDescription('')
  + g.panel.timeSeries.queryOptions.withTargets([
    g.query.prometheus.new(
      'prometheus', 'sum(dask_scheduler_workers) or vector(0)',
    ) + g.query.prometheus.withLegendFormat('Number of workers'),
  ])
  + g.panel.timeSeries.standardOptions.withDecimals(0)
  + g.panel.timeSeries.options.legend.withPlacement('right')
  + g.panel.timeSeries.options.tooltip.withMode('multi')
  + g.panel.timeSeries.standardOptions.withMin(0)
  + g.panel.timeSeries.fieldConfig.defaults.custom.withFillOpacity(6)
  + g.panel.timeSeries.fieldConfig.defaults.custom.withGradientMode('opacity'),

  hubResponseCodes:: g.panel.timeSeries.new('JupyterHub response status codes')
  + g.panel.timeSeries.panelOptions.withDescription('')
  + g.panel.timeSeries.queryOptions.withTargets([
    g.query.prometheus.new(
      'prometheus',
      |||
        sum(
          increase(
            jupyterhub_request_duration_seconds_bucket{
              job="jupyterhub",
              instance="cms.geddes.rcac.purdue.edu:80",
            }[2m]
          )
        ) by (code)
      |||
    ) + g.query.prometheus.withLegendFormat('{{ code }}'),
  ])
  + g.panel.timeSeries.fieldConfig.defaults.custom.withAxisWidth(0)
  + g.panel.timeSeries.options.legend.withPlacement('right')
  + g.panel.timeSeries.options.tooltip.withMode('multi')
  + g.panel.timeSeries.standardOptions.withMin(0)
  + g.panel.timeSeries.fieldConfig.defaults.custom.withFillOpacity(6)
  + g.panel.timeSeries.fieldConfig.defaults.custom.withGradientMode('opacity'),

  hubResponseLatency:: g.panel.timeSeries.new('JupyterHub response latency')
  + g.panel.timeSeries.panelOptions.withDescription('')
  + g.panel.timeSeries.queryOptions.withTargets([
    g.query.prometheus.new(
      'prometheus',
      |||
        histogram_quantile(
          0.99,
          sum(
            rate(
              jupyterhub_request_duration_seconds_bucket{
                job="jupyterhub",
                instance="cms.geddes.rcac.purdue.edu:80",
                handler!="jupyterhub.apihandlers.users.SpawnProgressAPIHandler"
              }[5m]
            )
          ) by (le))
      |||,
    ) + g.query.prometheus.withLegendFormat('99th percentile'),
    g.query.prometheus.new(
      'prometheus',
      |||
        histogram_quantile(
          0.5,
          sum(
            rate(
              jupyterhub_request_duration_seconds_bucket{
                job="jupyterhub",
                instance="cms.geddes.rcac.purdue.edu:80",
                handler!="jupyterhub.apihandlers.users.SpawnProgressAPIHandler"
              }[5m]
            )
          ) by (le))
      |||,
    ) + g.query.prometheus.withLegendFormat('50th percentile'),
    g.query.prometheus.new(
      'prometheus',
      |||
        histogram_quantile(
          0.25,
          sum(
            rate(
              jupyterhub_request_duration_seconds_bucket{
                job="jupyterhub",
                instance="cms.geddes.rcac.purdue.edu:80",
                handler!="jupyterhub.apihandlers.users.SpawnProgressAPIHandler"
              }[5m]
            )
          ) by (le))
      |||,
    ) + g.query.prometheus.withLegendFormat('25th percentile'),
  ])
  + g.panel.timeSeries.fieldConfig.defaults.custom.withAxisWidth(0)
  // + g.panel.timeSeries.options.legend.withPlacement('right')
  + g.panel.timeSeries.options.tooltip.withMode('multi')
  + g.panel.timeSeries.standardOptions.withMin(0)
  + g.panel.timeSeries.standardOptions.withUnit('s')
  + g.panel.timeSeries.fieldConfig.defaults.custom.withFillOpacity(6)
  + g.panel.timeSeries.fieldConfig.defaults.custom.withGradientMode('opacity'),

  serverStartTimes:: g.panel.timeSeries.new('User pod start times')
  + g.panel.timeSeries.panelOptions.withDescription('')
  + g.panel.timeSeries.queryOptions.withTargets([
    g.query.prometheus.new(
      'prometheus',
      |||
        histogram_quantile(1, sum(rate(jupyterhub_server_spawn_duration_seconds_bucket{
        job="jupyterhub",
        instance=~"cms(dev)?.geddes.rcac.purdue.edu:80"
        }[5m])) by (le))
      |||
    ) + g.query.prometheus.withLegendFormat('Server start time, s.'),
  ])
  + g.panel.timeSeries.fieldConfig.defaults.custom.withDrawStyle('points')
  + g.panel.timeSeries.fieldConfig.defaults.custom.thresholdsStyle.withMode('area')
  + g.panel.timeSeries.options.legend.withDisplayMode('hidden')
  + g.panel.timeSeries.options.tooltip.withMode('multi')
  + g.panel.timeSeries.standardOptions.withMin(0)
  + g.panel.timeSeries.standardOptions.withUnit('s')
  + g.panel.timeSeries.standardOptions.thresholds.withSteps([
    { color: 'green', value: 30},
    { color: 'yellow', value: 40},
    { color: 'orange', value: 75 },
    { color: 'red', value: 120 },
  ]),
}