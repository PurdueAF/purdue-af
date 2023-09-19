local g = import 'github.com/grafana/grafonnet/gen/grafonnet-latest/main.libsonnet';

{
    timeSeries(
        title='',
        description='',
        targets=[],
        transparent=false,
        unit=null,
        min=null,
        max=null,
        decimals=null,
        legendMode='list',
        legendPlacement=null,
        tooltipMode='multi',
        stackingMode=null,
        fillOpacity=6,
        gradientMode='opacity',
        axisWidth=null,
        drawStyle=null,
        thresholdMode=null,
        thresholdSteps=[],
    ):: g.panel.timeSeries.new(title)
        + g.panel.timeSeries.panelOptions.withDescription(description)
        + g.panel.timeSeries.queryOptions.withTargets(targets)
        + (if transparent then g.panel.timeSeries.panelOptions.withTransparent() else {})
        + g.panel.timeSeries.standardOptions.withUnit(unit)
        + g.panel.timeSeries.standardOptions.withMin(min)
        + g.panel.timeSeries.standardOptions.withMax(max)
        + g.panel.timeSeries.standardOptions.withDecimals(decimals)
        + g.panel.timeSeries.options.legend.withDisplayMode(legendMode)
        + g.panel.timeSeries.options.legend.withPlacement(legendPlacement)
        + g.panel.timeSeries.options.tooltip.withMode(tooltipMode)
        + g.panel.timeSeries.fieldConfig.defaults.custom.stacking.withMode(stackingMode)
        + g.panel.timeSeries.fieldConfig.defaults.custom.withFillOpacity(fillOpacity)
        + g.panel.timeSeries.fieldConfig.defaults.custom.withGradientMode(gradientMode)
        + g.panel.timeSeries.fieldConfig.defaults.custom.withAxisWidth(axisWidth)
        + g.panel.timeSeries.fieldConfig.defaults.custom.withDrawStyle(drawStyle)
        + g.panel.timeSeries.fieldConfig.defaults.custom.thresholdsStyle.withMode(thresholdMode)
        + g.panel.timeSeries.standardOptions.thresholds.withSteps(thresholdSteps),

    stat(
        title='',
        description='',
        targets=[],
        transparent=false,
        unit=null,
        colorMode=null
    ):: g.panel.stat.new(title)
        + g.panel.stat.panelOptions.withDescription(description)
        + g.panel.stat.queryOptions.withTargets(targets)
        + (if transparent then g.panel.gauge.panelOptions.withTransparent() else {})
        + g.panel.stat.standardOptions.withUnit(unit)
        + g.panel.stat.options.withColorMode(colorMode),
    
    gauge(
        title='',
        description='',
        targets=[],
        transparent=false,
        unit=null,
        min=null,
        max=null,
        decimals=null,
        thresholdMode=null,
        thresholdSteps=[],
    ):: g.panel.gauge.new(title)
        + g.panel.gauge.panelOptions.withDescription(description)
        + g.panel.gauge.queryOptions.withTargets(targets)
        + (if transparent then g.panel.gauge.panelOptions.withTransparent() else {})
        + g.panel.gauge.standardOptions.withUnit(unit)
        + g.panel.gauge.standardOptions.withMin(min)
        + g.panel.gauge.standardOptions.withMax(max)
        + g.panel.gauge.standardOptions.withDecimals(decimals)
        + g.panel.gauge.standardOptions.thresholds.withMode(thresholdMode)
        + g.panel.gauge.standardOptions.thresholds.withSteps(thresholdSteps),
}