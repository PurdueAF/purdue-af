local prometheus = import 'prometheus.libsonnet';
local panels = import 'panels.libsonnet';

{
  usersPerNamespace:: panels.timeSeries(
    title='Current users per namespace',
    targets=[
      prometheus.addQuery(
        'prometheus',
        'count by (namespace)(kube_pod_labels{pod=~"purdue-af-.*", namespace=~"cms(-dev)?"})',
        legendFormat='{{ namespace }}'
      ),
    ],
    min=0,
    decimals=0,
    legendPlacement='right',
    stackingMode='normal',
    fillOpacity=60,
    gradientMode=null
  ),

  usersPerNode:: panels.timeSeries(
    title='Current users per node',
    targets=[
      prometheus.addQuery(
        'prometheus',
        'count by (node)(kube_pod_info{namespace=~"cms(-dev)?", node!="", pod=~"purdue-af-.*"})',
        legendFormat='{{ node }}'
      ),
    ],
    min=0,
    decimals=0,
    legendPlacement='right',
    fillOpacity=20,
  ),

  nodeCpuUtil:: panels.timeSeries(
    description='% of available CPUs currently in use',
    targets=[
      prometheus.addQuery(
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
        legendFormat='{{ node }}'
      ),
    ],
    unit='percentunit',
    min=0,
    legendPlacement='right',
  ),

  nodeMemoryUtil:: panels.timeSeries(
    description='% of available memory currently in use',
    targets=[
      prometheus.addQuery(
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
        legendFormat='{{ node }}'
      ),
    ],
    unit='percentunit',
    min=0,
    legendPlacement='right',
  ),

  gpuGrEngineUtil:: panels.timeSeries(
    title='GPU Graphics Engine Utilization',
    targets=[
      prometheus.addQuery(
        'prometheus-rancher',
        'sum by (gpu, GPU_I_ID, GPU_I_PROFILE) (DCGM_FI_PROF_GR_ENGINE_ACTIVE{kubernetes_node="geddes-g000"})',
        legendFormat='Slice ID {{GPU_I_ID}}, GPU #{{gpu}}: {{GPU_I_PROFILE}}'
      ),
    ],
    unit='percentunit',
    min=0,
    legendPlacement='right',
  ),

  gpuMemUtil:: panels.timeSeries(
    title='GPU Memory Utilization',
    targets=[
      prometheus.addQuery(
        'prometheus-rancher',
        |||
          sum by (gpu, GPU_I_ID, GPU_I_PROFILE) (
            DCGM_FI_DEV_FB_USED{kubernetes_node="geddes-g000"} /
            ( DCGM_FI_DEV_FB_USED{kubernetes_node="geddes-g000"} + DCGM_FI_DEV_FB_FREE{kubernetes_node="geddes-g000"} )
          )
        |||,
        legendFormat='Slice ID {{GPU_I_ID}}, GPU #{{gpu}}: {{GPU_I_PROFILE}}'
      ),
    ],
    unit='percentunit',
    min=0,
    legendPlacement='right',
  ),

  gpuPower:: panels.timeSeries(
    title='GPU Power Usage (Watts)',
    targets=[
      prometheus.addQuery(
        'prometheus-rancher',
        'avg by (gpu) (avg_over_time(DCGM_FI_DEV_POWER_USAGE{kubernetes_node="geddes-g000"}[10m:10s]))',
        legendFormat='GPU #{{gpu}}'
      ),
    ],
    legendPlacement='right',
  ),

  tritonInferenceCount:: panels.timeSeries(
    title='Inferences per second (all Triton servers)',
    targets=[
      prometheus.addQuery(
        'prometheus',
        |||
          rate(
                (
                  sum(nv_inference_count{job="af-pod-monitor"}) by (model)
                )[1m:1s]
            )
        |||,
        legendFormat='{{ model }}'
      ),
    ],
    min=0,
    legendPlacement='right',
  ),

  tritonInferencesPerLB:: panels.timeSeries(
    title='Inferences per load balancer (all models)',
    targets=[
      prometheus.addQuery(
        'prometheus',
        |||
          rate(
                (
                    sum(nv_inference_count{job="af-pod-monitor"}) by (app)
                )[1m:1s]
            )
        |||,
        legendFormat='{{ app }}'
      ),
    ],
    min=0,
    legendPlacement='right',
  ),

  tritonNumServers:: panels.timeSeries(
    title='Triton servers per load balancer',
    targets=[
      prometheus.addQuery(
        'prometheus',
        |||
          sum by (deployment)(
              kube_deployment_status_replicas_available{namespace="cms", deployment=~"triton(.*)", deployment!="triton-nginx"}
          )
        |||,
        legendFormat='{{ deployment }}'
      ),
    ],
    min=0,
    legendPlacement='right',
  ),

  tritonLBmetric:: panels.timeSeries(
    title='Avg. queue wait time',
    description='(metric for autoscaling)',
    targets=[
      prometheus.addQuery(
        'prometheus',
        |||
          max(avg by (model) (delta(nv_inference_queue_duration_us{app="triton-triton"}[30s])/(1000 * (1 + delta(nv_inference_request_success{app="triton-triton"}[30s])))))
        |||,
        legendFormat='Max avg. by model queue wait'
      ),
    ],
    min=0,
    legendPlacement='bottom',
  ),

  daskSlurmSchedulers:: panels.timeSeries(
    title='Number of active Dask SLURM schedulers',
    targets=[
      prometheus.addQuery(
        'prometheus', 'count(dask_scheduler_workers)/4 or vector(0)',
        legendFormat='Number of schedulers'
      ),
    ],
    min=0,
    decimals=0,
    legendPlacement='right',
  ),

  daskSlurmWorkers:: panels.timeSeries(
    title='Number of Dask workers',
    targets=[
      prometheus.addQuery(
        'prometheus', 'sum(dask_scheduler_workers) or vector(0)',
        legendFormat='Number of workers'
      ),
    ],
    min=0,
    decimals=0,
    legendPlacement='right',
  ),

  hubResponseCodes:: panels.timeSeries(
    title='JupyterHub response status codes',
    targets=[
      prometheus.addQuery(
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
        |||,
        legendFormat='{{ code }}'
      ),
    ],
    min=0,
    legendPlacement='right',
    axisWidth=0
  ),

  hubResponseLatency:: panels.timeSeries(
    title='JupyterHub response latency',
    targets=[
      prometheus.addQuery(
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
        legendFormat='99th percentile'
      ),
      prometheus.addQuery(
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
        legendFormat='50th percentile'
      ),
      prometheus.addQuery(
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
        legendFormat='25th percentile'
      ),
    ],
    unit='s',
    min=0,
    legendPlacement='bottom',
    axisWidth=0
  ),

  serverStartTimes:: panels.timeSeries(
    title='User pod start times',
    targets=[
      prometheus.addQuery(
        'prometheus',
        |||
          histogram_quantile(1, sum(rate(jupyterhub_server_spawn_duration_seconds_bucket{
          job="jupyterhub",
          instance=~"cms(dev)?.geddes.rcac.purdue.edu:80"
          }[5m])) by (le))
        |||
      ),
    ],
    unit='s',
    min=0,
    legendMode='hidden',
    drawStyle='points',
    thresholdMode='area',
    thresholdSteps=[
      { color: 'green', value: 30 },
      { color: 'yellow', value: 40 },
      { color: 'orange', value: 75 },
      { color: 'red', value: 120 },
    ]
  ),
}
