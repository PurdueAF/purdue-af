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
    transparent=true,
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
    transparent=true,
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

  agcEventRate:: panels.timeSeries(
    title='Analysis Grand Challenge Event Rate per worker (1 file per dataset, 10 workers)',
    // description='',
    targets=[
      prometheus.addQuery(
        'prometheus', 'avg_over_time((sum(agc_event_rate_per_worker) > 0)[60m:])',
        legendFormat='Event rate', interval = '60m'
      ),
    ],
    min=0,
    unit='kHz',
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
    hideLegend=true,
    drawStyle='points',
    thresholdMode='area',
    thresholdSteps=[
      { color: 'green', value: 30 },
      { color: 'yellow', value: 40 },
      { color: 'orange', value: 75 },
      { color: 'red', value: 120 },
    ]
  ),

  nodeCpuStats:: panels.timeSeries(
    title='Node CPU usage (geddes-g000)',
    targets=[
      prometheus.addQuery(
        'prometheus',
        |||
          sum(kube_node_status_capacity{resource="cpu", node=~"geddes-g000.*"})
        |||,
        legendFormat='{{ node }} Total'
      ),
      prometheus.addQuery(
        'prometheus',
        |||
            sum(kube_pod_container_resource_requests{node="geddes-g000", resource="cpu"})
        |||,
        legendFormat='{{ node }} Requested'
      ),
      prometheus.addQuery(
        'prometheus',
        |||
          sum by (node)(rate(node_cpu_seconds_total{mode!="idle", instance=~"geddes-g000.*"}[5m]))
        |||,
        legendFormat='{{ node }} Used'
      ),
    ],
    min=0,
    legendPlacement='right',
    axisWidth=0,
    unit='CPU'
  ),

}
