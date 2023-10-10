local g = import 'github.com/grafana/grafonnet/gen/grafonnet-latest/main.libsonnet';
local prometheus = import 'prometheus.libsonnet';
local panels = import 'panels.libsonnet';

{
  nodeCpuUtilBarGauge:: panels.barGauge(
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
                  rate(node_cpu_seconds_total{mode!="idle"}[5m]),
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
    displayMode='lcd', orientation='horizontal',
    thresholdSteps=[
      { color: 'green', value: 0.0},
      { color: 'yellow', value: 60},
      { color: 'orange', value: 80 },
      { color: 'red', value: 90 },
    ]
  ),

  nodeMemUtilBarGauge:: panels.barGauge(
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
    displayMode='lcd', orientation='horizontal',
    thresholdSteps=[
      { color: 'green', value: 0.0},
      { color: 'yellow', value: 60},
      { color: 'orange', value: 80 },
      { color: 'red', value: 90 },
    ]
  ),
  // podStorageUtil:: panels.barGauge.new('Storage utilization (/home/<username>)')
  // + g.panel.barGauge.panelOptions.withDescription('')
  // + g.panel.barGauge.queryOptions.withTargets([
  //   g.query.prometheus.new(
  //     'prometheus',
  //     'af_home_dir_util{job="af-pod-monitor", username!="", pod!=""}',
  //   )
  //   + g.query.prometheus.withLegendFormat('{{username}} ({{pod}})')
  //   + g.query.prometheus.withInstant(),
  // ])
  // + g.panel.barGauge.options.withDisplayMode('lcd')
  // + g.panel.barGauge.options.withOrientation('horizontal')
  // + g.panel.barGauge.standardOptions.withDecimals(0)
  // + g.panel.barGauge.standardOptions.withUnit('percentunit')
  // + g.panel.barGauge.standardOptions.withMin(0)
  // + g.panel.barGauge.standardOptions.withMax(1)
  // + g.panel.barGauge.standardOptions.thresholds.withMode('absolute')
  // + g.panel.barGauge.standardOptions.thresholds.withSteps([
  //   { color: 'green', value: 0.0},
  //   { color: 'yellow', value: 0.60},
  //   { color: 'orange', value: 0.80},
  //   { color: 'red', value: 0.90},
  // ]),
}