local prometheus = import 'prometheus.libsonnet';
local panels = import 'panels.libsonnet';

{
  nodeCpuUtilGauge:: panels.gauge(
    title='Node CPU Utilization %',
    description='% of available CPUs currently in use',
    targets=[
      prometheus.addQuery(
        'prometheus',
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
        |||, legendFormat='{{ node }}', instant=true
      )
    ],
    unit='percentunit', min=0, max=1, thresholdMode='percentage',
    thresholdSteps=[
      { color: 'green', value: 0.0},
      { color: 'yellow', value: 60},
      { color: 'orange', value: 80 },
      { color: 'red', value: 90 },
    ]
  ),

  nodeMemUtilGauge:: panels.gauge(
    title='Node Memory Utilization %',
    description='% of available memory currently in use',
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
                  sum(node_memory_MemTotal_bytes) by (instance)
              ),
              "node", "$1", "instance", "(.*).rcac.purdue.edu:9796"
              ),
              "metric", "Memory", "node", "(.+)"
          )
        |||, legendFormat='{{ node }}', instant=true
      ),
    ],
    unit='percentunit', min=0, max=1, thresholdMode='percentage',
    thresholdSteps=[
      { color: 'green', value: 0.0},
      { color: 'yellow', value: 60},
      { color: 'orange', value: 80 },
      { color: 'red', value: 90 },
    ]
  ),

  gpuTemp:: panels.gauge(
    title='GPU Temperature',
    targets=[
      prometheus.addQuery(
        'prometheus-rancher',
        'avg (DCGM_FI_DEV_GPU_TEMP{kubernetes_node=~"geddes-g000|geddes-g001"}) by (gpu)',
        legendFormat='GPU #{{ gpu }}', instant=true
      ),
    ],
    transparent=true, unit='celsius', decimals=0, min=0, max=100,
    thresholdSteps=[
      { color: 'blue', value: 0},
      { color: 'green', value: 30},
      { color: 'yellow', value: 70},
      { color: 'orange', value: 80 },
      { color: 'red', value: 85 },
    ]
  ),
}