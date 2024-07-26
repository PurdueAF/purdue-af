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
        hideLegend=false,
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
        logBase=null,
    ):: g.panel.timeSeries.new(title)
        + g.panel.timeSeries.panelOptions.withDescription(description)
        + g.panel.timeSeries.queryOptions.withTargets(targets)
        + (if transparent then g.panel.timeSeries.panelOptions.withTransparent() else {})
        + g.panel.timeSeries.standardOptions.withUnit(unit)
        + g.panel.timeSeries.standardOptions.withMin(min)
        + g.panel.timeSeries.standardOptions.withMax(max)
        + g.panel.timeSeries.standardOptions.withDecimals(decimals)
        + (if hideLegend then
            g.panel.timeSeries.options.legend.withShowLegend(false)
            else (g.panel.timeSeries.options.legend.withDisplayMode(legendMode)+ g.panel.timeSeries.options.legend.withPlacement(legendPlacement)))
        + g.panel.timeSeries.options.tooltip.withMode(tooltipMode)
        + g.panel.timeSeries.fieldConfig.defaults.custom.stacking.withMode(stackingMode)
        + g.panel.timeSeries.fieldConfig.defaults.custom.withFillOpacity(fillOpacity)
        + g.panel.timeSeries.fieldConfig.defaults.custom.withGradientMode(gradientMode)
        + g.panel.timeSeries.fieldConfig.defaults.custom.withAxisWidth(axisWidth)
        + g.panel.timeSeries.fieldConfig.defaults.custom.withDrawStyle(drawStyle)
        + g.panel.timeSeries.fieldConfig.defaults.custom.thresholdsStyle.withMode(thresholdMode)
        + g.panel.timeSeries.standardOptions.thresholds.withSteps(thresholdSteps)
        + (if logBase != null then
            { fieldConfig+: {
                defaults+: {
                    custom+: {
                        scaleDistribution: {
                            type: 'log',
                            log: logBase,
                        },
                    },
                },
            }}
        else {}),

    stat(
        title='',
        description='',
        targets=[],
        transparent=false,
        unit=null,
        colorMode=null,
        thresholdMode='absolute',
        thresholdSteps=[
            {
            "value": null,
            "color": "green"
          },
        ],
    ):: g.panel.stat.new(title)
        + g.panel.stat.panelOptions.withDescription(description)
        + g.panel.stat.queryOptions.withTargets(targets)
        + (if transparent then g.panel.gauge.panelOptions.withTransparent() else {})
        + g.panel.stat.standardOptions.withUnit(unit)
        + g.panel.stat.options.withColorMode(colorMode)
        + g.panel.stat.standardOptions.thresholds.withMode(thresholdMode)
        + g.panel.stat.standardOptions.thresholds.withSteps(thresholdSteps),
    
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
    barGauge(
        title='',
        description='',
        targets=[],
        transparent=false,
        unit=null,
        min=null,
        max=null,
        decimals=null,
        displayMode=null,
        orientation=null,
        thresholdMode=null,
        thresholdSteps=[],
    ):: g.panel.barGauge.new(title)
        + g.panel.barGauge.panelOptions.withDescription(description)
        + g.panel.barGauge.queryOptions.withTargets(targets)
        + (if transparent then g.panel.barGauge.panelOptions.withTransparent() else {})
        + g.panel.barGauge.standardOptions.withUnit(unit)
        + g.panel.barGauge.standardOptions.withMin(min)
        + g.panel.barGauge.standardOptions.withMax(max)
        + g.panel.barGauge.standardOptions.withDecimals(decimals)
        + g.panel.barGauge.options.withDisplayMode(displayMode)
        + g.panel.barGauge.options.withOrientation(orientation)
        + g.panel.barGauge.standardOptions.thresholds.withMode(thresholdMode)
        + g.panel.barGauge.standardOptions.thresholds.withSteps(thresholdSteps),

    text(
        title='',
        description='',
        content='',
        transparent=false,
        mode='markdown'
    ):: g.panel.text.new(title)
        + g.panel.text.panelOptions.withDescription(description)
        + g.panel.text.options.withContent(content)
        + g.panel.text.options.withMode(mode)
        + (if transparent then g.panel.text.panelOptions.withTransparent() else {}),

    stateTimeline(
        title='',
        description='',
        targets=[],
        transparent=false,
        unit=null,
        colorMode='thresholds',
        showValue='auto',
        thresholdMode='absolute',
        thresholdSteps=[],
        hideLegend=true
    ):: g.panel.stateTimeline.new(title)
        + g.panel.stateTimeline.panelOptions.withDescription(description)
        + g.panel.stateTimeline.queryOptions.withTargets(targets)
        + (if transparent then g.panel.stateTimeline.panelOptions.withTransparent() else {})
        + g.panel.stateTimeline.standardOptions.withUnit(unit)
        + g.panel.stateTimeline.standardOptions.color.withMode(colorMode)
        + g.panel.stateTimeline.standardOptions.thresholds.withMode(thresholdMode)
        + g.panel.stateTimeline.standardOptions.thresholds.withSteps(thresholdSteps)
        + (if hideLegend then g.panel.stateTimeline.options.legend.withShowLegend(false))
        + g.panel.stateTimeline.options.withShowValue(showValue),

    statusHistory(
        title='',
        description='',
        targets=[],
        transparent=false,
        unit=null,
        showValue='auto',
        thresholdSteps=[],
        hideLegend = false
    ):: g.panel.statusHistory.new(title)
        + g.panel.statusHistory.panelOptions.withDescription(description)
        + g.panel.statusHistory.queryOptions.withTargets(targets)
        + (if transparent then g.panel.statusHistory.panelOptions.withTransparent() else {})
        + g.panel.statusHistory.standardOptions.withUnit(unit)
        + g.panel.statusHistory.options.withShowValue(showValue)
        + g.panel.statusHistory.standardOptions.thresholds.withSteps(thresholdSteps)
        + (if hideLegend then g.panel.statusHistory.options.legend.withShowLegend(false))
,
}