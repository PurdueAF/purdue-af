local g = import 'github.com/grafana/grafonnet/gen/grafonnet-latest/main.libsonnet';

{
  placeholder:: g.panel.text.new(''),
  placeholder_tr:: g.panel.text.new('') + g.panel.text.panelOptions.withTransparent()
}