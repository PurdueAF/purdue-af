local g = import 'github.com/grafana/grafonnet/gen/grafonnet-latest/main.libsonnet';

local myPanels = {
  row: import 'row.jsonnet',
  timeSeries: import 'timeSeries.jsonnet',
  stat: import 'stat.jsonnet',
  table: import 'table.jsonnet',
  gauge: import 'gauge.jsonnet',
  barGauge: import 'barGauge.jsonnet',
  heatmap: import 'heatmap.jsonnet',
  placeholder: import 'placeholder.jsonnet'
};

local w = g.panel.timeSeries.gridPos.withW;
local h = g.panel.timeSeries.gridPos.withH;

// local var =
//   g.dashboard.variable.query.new('pod')
//   + g.dashboard.variable.query.queryTypes.withLabelValues(
//     'pod',
//     'kube_pod_labels{namespace="cms",pod=~"purdue-af-.*"}',
//   )
//   + g.dashboard.variable.query.withDatasource(
//     type='prometheus',
//     uid='prometheus',
//   )
//   + g.dashboard.variable.query.selectionOptions.withIncludeAll()
//   + g.dashboard.variable.query.withSort(3)
// ;

local  userTable = g.panel.table.new('')
//   + g.panel.table.panelOptions.withTransparent()
  + g.panel.table.queryOptions.withTargets([
    g.query.prometheus.new(
      'prometheus',
      'af_home_dir_util{job="af-pod-monitor", username!=""}',
    )
    + g.query.prometheus.withRefId("storageUtil")
    + g.query.prometheus.withInstant()
    + g.query.prometheus.withFormat('table'),
    g.query.prometheus.new(
      'prometheus',
      'time() - af_home_dir_last_accessed{job="af-pod-monitor", username!=""}',
    )
    + g.query.prometheus.withRefId("storageLastAccess")
    + g.query.prometheus.withInstant()
    + g.query.prometheus.withFormat('table'),
    g.query.prometheus.new(
      'prometheus',
      'time() - kube_pod_created{namespace="cms",pod=~"purdue-af-.*"}',
    )
    + g.query.prometheus.withRefId("podAge")
    + g.query.prometheus.withInstant()
    + g.query.prometheus.withFormat('table'),
    g.query.prometheus.new(
      'prometheus',
      'kube_pod_container_resource_requests{namespace="cms",pod=~"purdue-af-.*",resource="cpu"}',
    )
    + g.query.prometheus.withRefId("cpuRequest")
    + g.query.prometheus.withInstant()
    + g.query.prometheus.withFormat('table'),
    g.query.prometheus.new(
      'prometheus',
      'kube_pod_container_resource_requests{namespace="cms",pod=~"purdue-af-.*",resource="memory"}',
    )
    + g.query.prometheus.withRefId("memRequest")
    + g.query.prometheus.withInstant()
    + g.query.prometheus.withFormat('table'),
    g.query.prometheus.new(
      'prometheus-rancher',
      |||
        sum(
            irate(container_cpu_usage_seconds_total{namespace="cms",pod=~"purdue-af-.*", container="notebook"}[5m])
        ) by (pod)
            /
        sum by (pod)(
            kube_pod_container_resource_requests{namespace="cms",pod=~"purdue-af-.*", resource="cpu", container="notebook"}
        )
      |||
    )
    + g.query.prometheus.withRefId("podCpuUtilCurrent")
    + g.query.prometheus.withInstant()
    + g.query.prometheus.withFormat('table'),
    g.query.prometheus.new(
      'prometheus-rancher',
      |||
        sum by (pod)(
            container_memory_working_set_bytes{namespace="cms", pod=~"purdue-af-.*", container="notebook"}
        ) /
        sum by (pod)(
            kube_pod_container_resource_requests{namespace="cms", pod=~"purdue-af-.*", resource="memory", container="notebook"}
        )
      |||
    )
    + g.query.prometheus.withRefId("podMemUtilCurrent")
    + g.query.prometheus.withInstant()
    + g.query.prometheus.withFormat('table'),
  ])
  + g.panel.table.queryOptions.withTransformations([
    g.panel.table.transformation.withId('joinByField')
    + g.panel.table.transformation.withOptions({"byField":"pod"}),
    g.panel.table.transformation.withId('filterFieldsByName')
    + g.panel.table.transformation.withOptions(
        {
            "include": {"pattern": "^(pod|username.*|Value.*|phase)$"},
            "exclude": {"pattern": "^(.*podStatus|username 2)$"}
        }
    ),
    g.panel.table.transformation.withId('organize')
    + g.panel.table.transformation.withOptions(
        {
            "indexByName": {
                "pod": 0,
                "username 1": 1,
                "Value #podAge": 2,
                "Value #storageUtil": 3,
                "Value #podCpuUtilCurrent": 4,
                "Value #podMemUtilCurrent": 5,
                "Value #cpuRequest": 6,
                "Value #memRequest": 7,
                "Value #storageLastAccess": 8
                },
        }
    ),
  ]
  )
  + g.panel.table.standardOptions.withOverrides(
    [
      g.panel.table.fieldOverride.byName.new("Value #storageUtil")
      + g.panel.table.fieldOverride.byName.withPropertiesFromOptions(
        g.panel.table.standardOptions.withDisplayName("/home/ storage utilization")
        + g.panel.table.standardOptions.withDecimals(1)
        + g.panel.table.standardOptions.withUnit("percentunit")
        + g.panel.table.standardOptions.withMin(0)
        + g.panel.table.standardOptions.withMax(1)
      )
      + g.panel.table.fieldOverride.byName.withProperty("thresholds", {
        "steps": [
            { color: 'green', value: 0.0},
            { color: 'yellow', value: 0.60},
            { color: 'orange', value: 0.80},
            { color: 'red', value: 0.90},
          ],
      })
      + g.panel.table.fieldOverride.byName.withProperty(
        "custom.cellOptions", {"type": "gauge", "mode": "lcd", "valueDisplayMode": "color"}
      ),
      g.panel.table.fieldOverride.byName.new("Value #storageLastAccess")
      + g.panel.table.fieldOverride.byName.withPropertiesFromOptions(
        g.panel.table.standardOptions.withDisplayName("/home/ storage last access")
        + g.panel.table.standardOptions.withUnit("s")
      ),
      g.panel.table.fieldOverride.byName.new("Value #podCpuUtilCurrent")
      + g.panel.table.fieldOverride.byName.withPropertiesFromOptions(
        g.panel.table.standardOptions.withDisplayName("CPU utililzation")
        + g.panel.table.standardOptions.withDecimals(1)
        + g.panel.table.standardOptions.withUnit("percentunit")
        + g.panel.table.standardOptions.withMin(0)
        + g.panel.table.standardOptions.withMax(1)
      )
      + g.panel.table.fieldOverride.byName.withProperty("thresholds", {
        "steps": [
            { color: 'green', value: 0.0},
            { color: 'yellow', value: 0.60},
            { color: 'orange', value: 0.80},
            { color: 'red', value: 0.90},
          ],
      })
      + g.panel.table.fieldOverride.byName.withProperty(
        "custom.cellOptions", {"type": "gauge", "mode": "lcd", "valueDisplayMode": "color"}
      ),
      g.panel.table.fieldOverride.byName.new("Value #podMemUtilCurrent")
      + g.panel.table.fieldOverride.byName.withPropertiesFromOptions(
        g.panel.table.standardOptions.withDisplayName("Memory utililzation")
        + g.panel.table.standardOptions.withDecimals(1)
        + g.panel.table.standardOptions.withUnit("percentunit")
        + g.panel.table.standardOptions.withMin(0)
        + g.panel.table.standardOptions.withMax(1)
      )
      + g.panel.table.fieldOverride.byName.withProperty("thresholds", {
        "steps": [
            { color: 'green', value: 0.0},
            { color: 'yellow', value: 0.60},
            { color: 'orange', value: 0.80},
            { color: 'red', value: 0.90},
          ],
      })
      + g.panel.table.fieldOverride.byName.withProperty(
        "custom.cellOptions", {"type": "gauge", "mode": "lcd", "valueDisplayMode": "color"}
      ),
      g.panel.table.fieldOverride.byName.new("Value #podAge")
      + g.panel.table.fieldOverride.byName.withPropertiesFromOptions(
        g.panel.table.standardOptions.withDisplayName("Pod age")
        + g.panel.table.standardOptions.withUnit("s")
      )
      + g.panel.table.fieldOverride.byName.withProperty("thresholds", {
        "steps": [
            { color: 'blue', value: 0},
            { color: 'green', value: 604800},
            { color: 'yellow', value: 1209600},
            { color: 'orange', value: 1814400},
            { color: 'red', value: 2419200},
        ]
    })
    + g.panel.table.fieldOverride.byName.withProperty(
        "custom.cellOptions", {"type": "color-text", "color": "value"}
    ),
    g.panel.table.fieldOverride.byName.new("Value #cpuRequest")
    + g.panel.table.fieldOverride.byName.withPropertiesFromOptions(
        g.panel.table.standardOptions.withDisplayName("CPU request")
        + g.panel.table.standardOptions.withUnit("CPUs")
    ),
    g.panel.table.fieldOverride.byName.new("Value #memRequest")
    + g.panel.table.fieldOverride.byName.withPropertiesFromOptions(
        g.panel.table.standardOptions.withDisplayName("Memory request")
        + g.panel.table.standardOptions.withUnit("bytes")
    ),
    g.panel.table.fieldOverride.byRegexp.new("/.*/")
    + g.panel.table.fieldOverride.byRegexp.withProperty("custom.align", "left")
    ]
  )
;


g.dashboard.new('Users Stats')
// + g.dashboard.withVariables([
//   var,
// ])
+ g.dashboard.withPanels([
  userTable + w(24) + h(24),
])
