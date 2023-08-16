local g = import 'github.com/grafana/grafonnet/gen/grafonnet-latest/main.libsonnet';

{
  podAgeDistribution:: g.panel.heatmap.new('Age of running user pods')
  + g.panel.heatmap.panelOptions.withDescription('')
  + g.panel.heatmap.queryOptions.withTargets([
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
    )
  ])
  + g.panel.heatmap.queryOptions.withInterval('600s')
  + g.panel.heatmap.options.withCalculate(true)
  + g.panel.heatmap.options.calculation.yBuckets.withValue(604800)
  + g.panel.heatmap.options.calculation.xBuckets.withMode('size')
  + g.panel.heatmap.options.calculation.xBuckets.withValue('600s')
  + g.panel.heatmap.options.legend.withShow(false)
  + g.panel.heatmap.options.tooltip.withShow()
  + g.panel.heatmap.options.yAxis.withUnit('s')
  + g.panel.heatmap.options.yAxis.withDecimals(0)
  + g.panel.heatmap.options.color.HeatmapColorOptions.withMode('scheme')
  + g.panel.heatmap.options.color.HeatmapColorOptions.withScheme('Viridis'),

  podStorageUtil:: g.panel.heatmap.new('Storage utilization (/home/<username>)')
  + g.panel.heatmap.panelOptions.withDescription('')
  + g.panel.heatmap.queryOptions.withTargets([
    g.query.prometheus.new(
      'prometheus',
      'af_home_dir_util{job="af-pod-monitor"}',
    )
  ])
  + g.panel.heatmap.queryOptions.withInterval('600s')
  + g.panel.heatmap.options.withCalculate(true)
  + g.panel.heatmap.options.calculation.yBuckets.withValue(0.1)
  + g.panel.heatmap.options.calculation.xBuckets.withMode('size')
  + g.panel.heatmap.options.calculation.xBuckets.withValue('600s')
  + g.panel.heatmap.options.legend.withShow(false)
  + g.panel.heatmap.options.tooltip.withShow()
  + g.panel.heatmap.options.yAxis.withUnit('percentunit')
  + g.panel.heatmap.options.yAxis.withMin(0)
  + g.panel.heatmap.options.yAxis.withMax(1)
  + g.panel.heatmap.options.color.HeatmapColorOptions.withMode('scheme')
  + g.panel.heatmap.options.color.HeatmapColorOptions.withScheme('Viridis')
}