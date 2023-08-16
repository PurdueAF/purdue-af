local g = import 'github.com/grafana/grafonnet/gen/grafonnet-latest/main.libsonnet';

{


//   transform='timeseries_to_rows',
//   datasource='$PROMETHEUS_DS',
//   styles=[
//       {pattern: 'GPU_I_PROFILE', type: 'string', alias: 'GPU slice'},
//       {pattern: 'Value', type: 'number', alias: 'Number of slices'},
//   ],
// ).addTarget(
//   prometheus.target(
//     format='table'
//   ),
// ).hideColumn('Time');
  gpuSlices:: g.panel.table.new('')
//   + g.panel.table.panelOptions.withTransparent()
  + g.panel.table.queryOptions.withTargets([
    g.query.prometheus.new(
      'prometheus',
      'count (DCGM_FI_DEV_GPU_TEMP) by (GPU_I_PROFILE)',
    )
    + g.query.prometheus.withLegendFormat('{{GPU_I_PROFILE}}')
    + g.query.prometheus.withInstant()
    + g.query.prometheus.withFormat('table')
  ])
  + g.panel.table.queryOptions.withTransformations([
    g.panel.table.transformation.withId('organize')
    + g.panel.table.transformation.withOptions({"excludeByName": {"Time": true}})
  ])
  + g.panel.table.queryOptions.withTransformations([
    g.panel.table.transformation.withId('sortBy')
    + g.panel.table.transformation.withOptions({"sort": [{"field": "GPU_I_PROFILE"}]})
  ])
  + g.panel.table.standardOptions.withOverrides(
    [
      g.panel.table.fieldOverride.byName.new("GPU_I_PROFILE")
      + g.panel.table.fieldOverride.byName.withPropertiesFromOptions(
        g.panel.table.standardOptions.withDisplayName("GPU slice")
      ),
      g.panel.table.fieldOverride.byName.new("Value")
      + g.panel.table.fieldOverride.byName.withPropertiesFromOptions(
        g.panel.table.standardOptions.withDisplayName("Number of slices")
      ),
      g.panel.table.fieldOverride.byRegexp.new("/.*/")
      + g.panel.table.fieldOverride.byRegexp.withProperty("custom.align", "left")
    ]
  ),


// local tritonTable= tablePanel.new(
//   '',
//   transform='timeseries_to_rows',
//   transparent=true,
//   datasource='$PROMETHEUS_DS',
//   styles=[
//       {pattern: 'name', type: 'string', alias: 'Load balancer'},
//       {pattern: 'Value #A', type: 'number', alias: '# servers'},
//       {pattern: 'Value #B', type: 'number', alias: '# models'},
//   ],
// )
// .addTargets([
//   prometheus.target(
//     |||
//       label_replace(
//       sum by (name)(label_replace(
//           kube_deployment_status_replicas_available{namespace="cms", deployment=~"triton-(.*)"},
//           "name", "$1", "deployment", "(.*)"
//       )),
//       "name", "$1-triton", "name", "(.*)-nginx"
//       )
//     |||,
//     legendFormat="{{name}}", instant=true, format='table'
//   ),
//   prometheus.target(
//     |||
//       count by (name) (
//         count by (model, name) (
//           label_replace(nv_inference_count{job="af-pod-monitor"}, "name", "$1", "app", "(.*)")
//         )
//       )
//     |||,
//     legendFormat="{{name}}", instant=true, format='table'
//     ),
// ]).hideColumn('Time');
    tritonTable:: g.panel.table.new(''),
}