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

local addQueryTableInstant(datasource, query, refId) = 
  g.query.prometheus.new(datasource, query)
  + g.query.prometheus.withRefId(refId)
  + g.query.prometheus.withInstant()
  + g.query.prometheus.withFormat('table');

local configureColumn(old_name, new_name, unit=null, min=null, max=null, decimals=null, columnWidth=null) =
  g.panel.table.fieldOverride.byName.new(old_name)
  + g.panel.table.fieldOverride.byName.withPropertiesFromOptions(
    g.panel.table.standardOptions.withDisplayName(new_name)
    + g.panel.table.standardOptions.withUnit(unit)
    + g.panel.table.standardOptions.withMin(min)
    + g.panel.table.standardOptions.withMax(max)
    + g.panel.table.standardOptions.withDecimals(decimals)
  )
  + g.panel.table.fieldOverride.byName.withProperty("custom.width", columnWidth)
;

local configureBarGauge(steps) =
  g.panel.table.fieldOverride.byName.withProperty("thresholds", {"steps": steps})
  + g.panel.table.fieldOverride.byName.withProperty(
    "custom.cellOptions", {"type": "gauge", "mode": "lcd", "valueDisplayMode": "color"}
  );

local var =
  g.dashboard.variable.query.new('namespace')
  + g.dashboard.variable.query.queryTypes.withLabelValues(
    'namespace',
    'kube_pod_labels{namespace=~"cms(-dev)?",pod=~"purdue-af-.*"}',
  )
  + g.dashboard.variable.query.withDatasource(
    type='prometheus',
    uid='prometheus',
  )
  + g.dashboard.variable.query.withSort(3)
;

local  userTable = g.panel.table.new('')
//   + g.panel.table.panelOptions.withTransparent()
  + g.panel.table.queryOptions.withTargets([
    addQueryTableInstant(
      'prometheus',
      'af_home_dir_util{namespace=~"$namespace",job="af-pod-monitor",username!=""}',
      'storageUtil'
    ),
    addQueryTableInstant(
      'prometheus',
      'time() - af_home_dir_last_accessed{namespace=~"$namespace", job="af-pod-monitor",username!=""}',
      'storageLastAccess'
    ),
    addQueryTableInstant(
      'prometheus',
      |||
        label_replace(
          time() - kube_pod_created{namespace=~"$namespace",pod=~"purdue-af-.*"},
          "userId", "$1", "pod", "purdue-af-(.*)"
        )
      |||,
      'podAge'
    ),
    addQueryTableInstant(
      'prometheus',
      'kube_pod_container_resource_requests{namespace=~"$namespace",pod=~"purdue-af-.*",resource="cpu"}',
      'cpuRequest'
    ),
    addQueryTableInstant(
      'prometheus',
      'kube_pod_container_resource_requests{namespace=~"$namespace",pod=~"purdue-af-.*",resource="memory"}',
      'memRequest'
    ),
    addQueryTableInstant(
      'prometheus-rancher',
      |||
        sum(
            irate(container_cpu_usage_seconds_total{namespace=~"$namespace",pod=~"purdue-af-.*", container="notebook"}[5m])
        ) by (pod)
            /
        sum by (pod)(
            kube_pod_container_resource_requests{namespace=~"$namespace",pod=~"purdue-af-.*", resource="cpu", container="notebook"}
        )
      |||,
      'podCpuUtilCurrent'
    ),
    addQueryTableInstant(
      'prometheus-rancher',
      |||
        sum by (pod)(
            container_memory_working_set_bytes{namespace=~"$namespace", pod=~"purdue-af-.*", container="notebook"}
        ) /
        sum by (pod)(
            kube_pod_container_resource_requests{namespace=~"$namespace", pod=~"purdue-af-.*", resource="memory", container="notebook"}
        )
      |||,
      'podMemUtilCurrent'
    ),
  ])
  + g.panel.table.queryOptions.withTransformations([
    // join into a single table
    g.panel.table.transformation.withId('joinByField')
    + g.panel.table.transformation.withOptions({"byField":"pod"}),
    // filter columns
    g.panel.table.transformation.withId('filterFieldsByName')
    + g.panel.table.transformation.withOptions(
        {
            "include": {"pattern": "^(pod|username.*|Value.*|userId|node 3)$"},
            "exclude": {"pattern": "^(.*podStatus|username 2|Value #userId)$"}
        }
    ),
    // set order of columns
    g.panel.table.transformation.withId('organize')
    + g.panel.table.transformation.withOptions(
        {
            "indexByName": {
                "userId": 0,
                "pod": 1,
                "username 1": 2,
                "Value #podAge": 3,
                "Value #storageUtil": 4,
                "Value #podCpuUtilCurrent": 5,
                "Value #podMemUtilCurrent": 6,
                "Value #cpuRequest": 7,
                "Value #memRequest": 8,
                "Value #storageLastAccess": 9,
                "node 3": 10,
                },
        }
    ),
    // convert user ID to int
    g.panel.table.transformation.withId('convertFieldType')
    + g.panel.table.transformation.withOptions(
        {
          "conversions": [
            {
              "targetField": "userId",
              "destinationType": "number"
            }
          ]
        }
    ),
  ])
  + g.panel.table.standardOptions.withOverrides([
    configureColumn("userId", "ID", columnWidth=40),
    configureColumn("pod", "Pod", columnWidth=120),
    configureColumn("node", "Node", columnWidth=120),
    configureColumn("username", "Username", columnWidth=150),
    configureColumn("Value #podAge", "Pod age", "s", columnWidth=120)
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
    configureColumn("Value #storageUtil", "Storage utilization", "percentunit", 0, 1, 1)
    + configureBarGauge([
      { color: 'green', value: 0.0},
      { color: 'yellow', value: 0.60},
      { color: 'orange', value: 0.80},
      { color: 'red', value: 0.90},
    ]),
    configureColumn("Value #podCpuUtilCurrent", "CPU utilization", "percentunit", 0, 1, 1)
    + configureBarGauge([
      { color: 'green', value: 0.0},
      { color: 'yellow', value: 0.60},
      { color: 'orange', value: 0.80},
      { color: 'red', value: 0.90},
    ]),
    configureColumn("Value #podMemUtilCurrent", "Memory utilization", "percentunit", 0, 1, 1)
    + configureBarGauge([
      { color: 'green', value: 0.0},
      { color: 'yellow', value: 0.60},
      { color: 'orange', value: 0.80},
      { color: 'red', value: 0.90},
    ]),
    configureColumn("Value #cpuRequest", "CPU request", columnWidth=120),
    configureColumn("Value #memRequest", "Memory request", "bytes", columnWidth=150),
    configureColumn("Value #storageLastAccess", "Last accessed /home/", "s", columnWidth=180)
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
    g.panel.table.fieldOverride.byRegexp.new(".*")
    + g.panel.table.fieldOverride.byRegexp.withProperty("custom.align", "left"),
    g.panel.table.fieldOverride.byRegexp.new(".*request")
    + g.panel.table.fieldOverride.byRegexp.withProperty("custom.align", "center")
    ]
  )
  + g.panel.table.options.withSortBy([{"displayName": "ID", "desc": false}])
;


g.dashboard.new('Users Statistics')
+ g.dashboard.withVariables([
  var,
])
+ g.dashboard.withUid('user-stats-dashboard')
+ g.dashboard.withDescription('Purdue AF User Statistics')
+ g.dashboard.withLiveNow()
+ g.dashboard.withRefresh('10s')
+ g.dashboard.withStyle(value="dark")
+ g.dashboard.withTimezone(value="browser")
// + g.dashboard.time.withFrom(value="now-6h")
// + g.dashboard.time.withTo(value="now")
+ g.dashboard.graphTooltip.withSharedCrosshair()
+ g.dashboard.withPanels([
  userTable + w(24) + h(24),
])
