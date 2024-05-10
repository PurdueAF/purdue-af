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
    title='Node CPU utilization %',
    targets=[
      prometheus.addQuery(
        'prometheus',
        |||
          sum by (node)(
            label_replace(
              label_replace(
                rate(node_cpu_seconds_total{mode!="idle"}[5m]),
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
    title='Node memory utilization %',
    targets=[
      prometheus.addQuery(
        'prometheus',
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
                sum(node_memory_MemTotal_bytes{instance!="hammer-adm.rcac.purdue.edu:9100"}) by (instance)
              ),
              "node", "$1", "instance", "(.*).rcac.purdue.edu:9796"
            ),
            "node", "$1", "node", "(.*).cms"
          )
        |||,
        legendFormat='{{ node }}'
      ),
    ],
    unit='percentunit',
    min=0,
    legendPlacement='right',
  ),

  nodeCpuRequest:: panels.timeSeries(
    title='CPUs requested by users',
    targets=[
      prometheus.addQuery(
        'prometheus',
        |||
          sum by (node) (
            kube_pod_container_resource_requests{
              namespace=~"cms(-dev)?",
              pod=~"purdue-af-.*",
              container="notebook",
              resource="cpu"
            }
          )
        |||,
        legendFormat='{{ node }}'
      ),
    ],
    unit='CPUs',
    min=0,
    legendPlacement='right',
  ),

  nodeMemoryRequest:: panels.timeSeries(
    title='Memory requested by users',
    targets=[
      prometheus.addQuery(
        'prometheus',
        |||
          sum by (node) (
            kube_pod_container_resource_requests{
              namespace=~"cms(-dev)?",
              pod=~"purdue-af-.*",
              container="notebook",
              resource="memory"
            }
          )
        |||,
        legendFormat='{{ node }}'
      ),
    ],
    unit='bytes',
    min=0,
    legendPlacement='right',
  ),

  nodeEphStorageUsage:: panels.timeSeries(
    title='Ephemeral storage usage',
    targets=[
      prometheus.addQuery(
        'prometheus',
        |||
          100-ephemeral_storage_node_percentage{node_name=~"geddes-b013|geddes-b014|geddes-b015|geddes-g000|geddes-g001|geddes-g002|vm-hammer-.*"}
        |||,
        legendFormat='{{ node_name }}'
      ),
    ],
    unit='percent',
    min=0, max=100,
    legendPlacement='right',
  ),

  gpuGrEngineUtil:: panels.timeSeries(
    title='GPU Graphics Engine Utilization',
    targets=[
      prometheus.addQuery(
        'prometheus-rancher',
        'sum by (gpu, GPU_I_ID, GPU_I_PROFILE, kubernetes_node) (DCGM_FI_PROF_GR_ENGINE_ACTIVE)',
        legendFormat='{{kubernetes_node}} {{GPU_I_PROFILE}}'
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
          sum by (gpu, GPU_I_ID, GPU_I_PROFILE, kubernetes_node) (
            DCGM_FI_DEV_FB_USED /
            ( DCGM_FI_DEV_FB_USED + DCGM_FI_DEV_FB_FREE )
          )
        |||,
        legendFormat='{{kubernetes_node}} {{GPU_I_PROFILE}}'
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
        'avg by (gpu, kubernetes_node) (avg_over_time(DCGM_FI_DEV_POWER_USAGE[10m:1m]))',
        legendFormat='{{kubernetes_node}}'
      ),
    ],
    legendPlacement='right',
  ),

  tritonInferenceCount:: panels.timeSeries(
    title='Inferences per second (all Triton servers)',
    targets=[
      prometheus.addQuery(
        'prometheus-rancher',
        |||
          rate(
                (
                  sum(nv_inference_count) by (model, version)
                )[5m:1m]
            )
        |||,
        legendFormat='{{ model }} {{ version }}'
      ),
    ],
    min=0,
    legendPlacement='right',
  ),

  tritonInferencesPerLB:: panels.timeSeries(
    title='Inferences per load balancer (all models)',
    targets=[
      prometheus.addQuery(
        'prometheus-rancher',
        |||
          rate(
                (
                    sum(nv_inference_count) by (service)
                )[5m:1m]
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
        'prometheus-rancher',
        |||
          sum by (deployment)(
              kube_deployment_status_replicas_available{namespace="cms", deployment=~"triton(.*)lb"}
          )
        |||,
        legendFormat='{{ deployment }}'
      ),
    ],
    min=0,
    legendPlacement='right',
  ),

  tritonServerSaturation:: panels.timeSeries(
    title='Triton load balancer saturation metric',
    targets=[
      prometheus.addQuery(
        'prometheus',
        |||
          sonic_lb_saturated{lb_name=~"triton-.*-lb"}
        |||,
        legendFormat='{{ lb_name }}'
      ),
    ],
    min=0,
    legendPlacement='right',
  ),


  tritonQueueTimeByModel:: panels.timeSeries(
    title='Queue time by model',
    description='',
    targets=[
      prometheus.addQuery(
        'prometheus-rancher',
        |||
          sum by (model, version) (
            avg by (model, lb_name, version) (
              label_replace(irate(nv_inference_queue_duration_us{pod=~"triton-.*"}[5m]), "lb_name", "$1", "pod", "(.*)-(.*)-(.*)$")
              /
              (1000 * (1 + 
                label_replace(irate(nv_inference_request_success{pod=~"triton-.*"}[5m]), "lb_name", "$1", "pod", "(.*)-(.*)-(.*)$")
              ))
            )
          )
        |||,
        legendFormat='{{ model }} {{ version }}'
      ),
    ],
    min=0,
    legendPlacement='right',
  ),

  tritonQueueTimeByServer:: panels.timeSeries(
    title='Max queue time by load balancer',
    description='',
    targets=[
      prometheus.addQuery(
        'prometheus-rancher',
        |||
          max by (lb_name) (
            avg by (model, lb_name, version) (
              label_replace(irate(nv_inference_queue_duration_us{pod=~"triton-.*"}[5m]), "lb_name", "$1", "pod", "(.*)-(.*)-(.*)$")
              /
              (1000 * (1 + 
                label_replace(irate(nv_inference_request_success{pod=~"triton-.*"}[5m]), "lb_name", "$1", "pod", "(.*)-(.*)-(.*)$")
              ))
            )
          )
        |||,
        legendFormat='{{ lb_name }}'
      ),
    ],
    min=0,
    legendPlacement='right',
  ),

  tritonGPUload:: panels.timeSeries(
    title='GPU utilization per SONIC load balancer (avg over 5m)',
    description='',
    targets=[
      prometheus.addQuery(
        'prometheus-rancher',
        |||
          label_replace(avg_over_time(nv_gpu_utilization[5m]), "app", "$1", "pod", "(.*)-(.*)-(.*)$")
        |||,
        legendFormat='{{ pod }}'
      ),
    ],
    min=0,
    unit='percentunit',
    legendPlacement='right',
  ),


  tritonMemUtil:: panels.timeSeries(
    title='Memory utilization by Triton pods',
    description='',
    targets=[
      prometheus.addQuery(
        'prometheus-rancher',
        |||
          sum by (pod)(
              container_memory_working_set_bytes{namespace="cms", pod=~"triton-.*", container=~"triton-.*"}
          ) /
          sum by (pod)(
              kube_pod_container_resource_requests{namespace="cms", pod=~"triton-.*", container=~"triton-.*", resource="memory"}
          )
        |||,
        legendFormat='{{ pod }}'
      ),
    ],
    unit='percentunit',
    min=0, max=1,
    legendPlacement='right',
  ),

  daskSchedulers:: panels.timeSeries(
    title='Number of Dask Gateway / k8s schedulers',
    targets=[
      prometheus.addQuery(
        'prometheus', 'count by (user) (sum by (user, instance) (dask_scheduler_workers))',
        legendFormat='{{ user }}'
      ),
    ],
    min=0,
    decimals=0,
    legendPlacement='right',
  ),

  daskWorkers:: panels.timeSeries(
    title='Number of Dask Gateway / k8s workers',
    targets=[
      prometheus.addQuery(
        'prometheus', 'sum by (user) (dask_scheduler_workers)',
        legendFormat='{{ user }}'
      ),
    ],
    min=0,
    decimals=0,
    legendPlacement='right',
  ),

  daskClients:: panels.timeSeries(
    title='Number of Dask Gateway / k8s clients',
    targets=[
      prometheus.addQuery(
        'prometheus', 'sum by (user) (dask_scheduler_clients)',
        legendFormat='{{ user }}'
      ),
    ],
    min=0,
    decimals=0,
    legendPlacement='right',
  ),

  hammerSlurmJobs:: panels.timeSeries(
    title='Slurm jobs (Hammer)',
    targets=[
      prometheus.addQuery(
        'prometheus', 'slurm_info_job_user{user!="cmspilot",user!="lcgadmin",user!="uscmslcl"}',
        legendFormat='{{user}}'
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
