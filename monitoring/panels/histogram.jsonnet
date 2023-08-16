local g = import 'github.com/grafana/grafonnet/gen/grafonnet-latest/main.libsonnet';

{
  podAgeDistribution:: g.panel.histogram.new('Age of running user pods')
  + g.panel.histogram.panelOptions.withDescription('')
  + g.panel.histogram.queryOptions.withTargets([
    g.query.prometheus.new(
      'prometheus',
      |||
        (
            time()
            - (
            kube_pod_created{pod=~"purdue-af-.*", namespace=~"cms(-dev)?"}
            )
        )
      |||,
    ) + g.query.prometheus.withInstant(),
  ])
  + g.panel.histogram.options.withBucketSize('604800')
  + g.panel.histogram.options.withCombine()
  + g.panel.histogram.options.legend.withDisplayMode('hidden')
  + g.panel.histogram.options.tooltip.withMode('single')
  + g.panel.histogram.options.tooltip.withSort('none')
  + g.panel.histogram.standardOptions.withUnit('s')
  + g.panel.histogram.standardOptions.withMin(0)
  + g.panel.histogram.fieldConfig.defaults.custom.withFillOpacity(60)
  + g.panel.histogram.fieldConfig.defaults.custom.withGradientMode('hue'),

  podStorageUtil:: g.panel.histogram.new('/home/ storage utilization')
  + g.panel.histogram.panelOptions.withDescription('')
  + g.panel.histogram.queryOptions.withTargets([
    g.query.prometheus.new(
      'prometheus',
      'af_home_dir_util{job="af-pod-monitor"}',
    ) + g.query.prometheus.withInstant(),
  ])
  + g.panel.histogram.fieldConfig.defaults.custom.withAxisSoftMin(0)
  + g.panel.histogram.fieldConfig.defaults.custom.withAxisSoftMax(1)
  + g.panel.histogram.options.withCombine()
  + g.panel.histogram.options.legend.withDisplayMode('hidden')
  + g.panel.histogram.standardOptions.withUnit('percentunit')
  + g.panel.histogram.standardOptions.withMin(0)
  + g.panel.histogram.standardOptions.withMax(1)
  + g.panel.histogram.fieldConfig.defaults.custom.withFillOpacity(60)
  + g.panel.histogram.fieldConfig.defaults.custom.withGradientMode('hue'),
}