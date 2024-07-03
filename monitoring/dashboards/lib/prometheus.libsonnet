local g = import 'github.com/grafana/grafonnet/gen/grafonnet-latest/main.libsonnet';

{
    addQuery(
        datasource,
        query,
        refId='',
        format=null,
        legendFormat='',
        instant=false,
        interval=''
    ):: 
        g.query.prometheus.new(datasource, query)
        + g.query.prometheus.withRefId(refId)
        + g.query.prometheus.withFormat(format)
        + g.query.prometheus.withLegendFormat(legendFormat)
        + g.query.prometheus.withInterval(interval)
        + (if instant then g.query.prometheus.withInstant() else {})
}