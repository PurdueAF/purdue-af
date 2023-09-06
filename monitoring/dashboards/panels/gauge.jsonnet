local g = import 'github.com/grafana/grafonnet/gen/grafonnet-latest/main.libsonnet';

{
  nodeCpuUtilGauge:: g.panel.gauge.new('Node CPU Utilization %')
  + g.panel.gauge.panelOptions.withDescription('% of available CPUs currently in use')
  // + g.panel.gauge.panelOptions.withTransparent()
  + g.panel.gauge.queryOptions.withTargets([
    g.query.prometheus.new(
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
      |||,
    )
    + g.query.prometheus.withLegendFormat('{{ node }}')
    + g.query.prometheus.withInstant(),
  ])
  + g.panel.gauge.standardOptions.withUnit('percentunit')
  + g.panel.gauge.standardOptions.withMin(0)
  + g.panel.gauge.standardOptions.withMax(1)
  + g.panel.gauge.standardOptions.thresholds.withMode('percentage')
  + g.panel.gauge.standardOptions.thresholds.withSteps([
    { color: 'green', value: 0.0},
    { color: 'yellow', value: 60},
    { color: 'orange', value: 80 },
    { color: 'red', value: 90 },
  ]),

  nodeMemUtilGauge:: g.panel.gauge.new('Node Memory Utilization %')
  + g.panel.gauge.panelOptions.withDescription('% of available memory currently in use')
  // + g.panel.gauge.panelOptions.withTransparent()
  + g.panel.gauge.queryOptions.withTargets([
    g.query.prometheus.new(
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
    |||,
    )
    + g.query.prometheus.withLegendFormat('{{ node }}')
    + g.query.prometheus.withInstant(),
  ])
  + g.panel.gauge.standardOptions.withUnit('percentunit')
  + g.panel.gauge.standardOptions.withMin(0)
  + g.panel.gauge.standardOptions.withMax(1)
  + g.panel.gauge.standardOptions.thresholds.withMode('percentage')
  + g.panel.gauge.standardOptions.thresholds.withSteps([
    { color: 'green', value: 0.0},
    { color: 'yellow', value: 60},
    { color: 'orange', value: 80 },
    { color: 'red', value: 90 },
  ]),

// local gpuTemp = gaugePanel.new(
//   showThresholdLabels=false,

// ]).addThresholds(
//   [
//     { color: 'blue', value: 0},
//     { color: 'green', value: 30},
//     { color: 'yellow', value: 70},
//     { color: 'orange', value: 80 },
//     { color: 'red', value: 85 },
//     ]
// );
  gpuTemp:: g.panel.gauge.new('GPU Temperature')
  + g.panel.gauge.panelOptions.withDescription('')
  + g.panel.gauge.panelOptions.withTransparent()
  + g.panel.gauge.queryOptions.withTargets([
    g.query.prometheus.new(
    'prometheus-rancher', 'avg (DCGM_FI_DEV_GPU_TEMP{kubernetes_node="geddes-g000"}) by (gpu)',
    )
    + g.query.prometheus.withLegendFormat('GPU #{{ gpu }}')
    + g.query.prometheus.withInstant(),
  ])
  + g.panel.gauge.standardOptions.withDecimals(0)
  + g.panel.gauge.standardOptions.withUnit('celsius')
  + g.panel.gauge.standardOptions.withMin(0)
  + g.panel.gauge.standardOptions.withMax(100)
//   + g.panel.gauge.standardOptions.thresholds.withMode('percentage')
  + g.panel.gauge.standardOptions.thresholds.withSteps([
    { color: 'blue', value: 0},
    { color: 'green', value: 30},
    { color: 'yellow', value: 70},
    { color: 'orange', value: 80 },
    { color: 'red', value: 85 },
  ]),

}