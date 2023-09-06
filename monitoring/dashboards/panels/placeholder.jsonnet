local g = import 'github.com/grafana/grafonnet/gen/grafonnet-latest/main.libsonnet';

local box = g.panel.text.new(' ')+ g.panel.timeSeries.queryOptions.withTargets([g.query.prometheus.new('prometheus', '')]);

{
  placeholder:: box,
  placeholder_tr:: box + g.panel.text.panelOptions.withTransparent(),
}