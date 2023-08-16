local g = import 'github.com/grafana/grafonnet/gen/grafonnet-latest/main.libsonnet';

{
  podStorageUtil:: g.panel.barGauge.new('Storage utilization (/home/<username>)')
  + g.panel.barGauge.panelOptions.withDescription('')
  + g.panel.barGauge.queryOptions.withTargets([
    g.query.prometheus.new(
      'prometheus',
      'af_home_dir_util{job="af-pod-monitor", username!="", pod!=""}',
    )
    + g.query.prometheus.withLegendFormat('{{username}} ({{pod}})')
    + g.query.prometheus.withInstant(),
  ])
  + g.panel.barGauge.options.withDisplayMode('lcd')
  + g.panel.barGauge.options.withOrientation('horizontal')
  + g.panel.barGauge.standardOptions.withDecimals(0)
  + g.panel.barGauge.standardOptions.withUnit('percentunit')
  + g.panel.barGauge.standardOptions.withMin(0)
  + g.panel.barGauge.standardOptions.withMax(1)
  + g.panel.barGauge.standardOptions.thresholds.withMode('absolute')
  + g.panel.barGauge.standardOptions.thresholds.withSteps([
    { color: 'green', value: 0.0},
    { color: 'yellow', value: 0.60},
    { color: 'orange', value: 0.80},
    { color: 'red', value: 0.90},
  ]),
}