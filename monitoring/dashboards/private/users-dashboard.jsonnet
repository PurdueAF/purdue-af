local g = import 'github.com/grafana/grafonnet/gen/grafonnet-latest/main.libsonnet';
local prometheus = import 'prometheus.libsonnet';

local w = g.panel.table.gridPos.withW;
local h = g.panel.table.gridPos.withH;

local utilizationThresholds = [
  { color: 'green', value: 0.0},
  { color: 'yellow', value: 0.60},
  { color: 'orange', value: 0.80},
  { color: 'red', value: 0.90},
];

local ageThresholds = [
  { color: 'blue', value: 0},
  { color: 'green', value: 86400}, # 1 day
  { color: 'yellow', value: 1209600}, # 2 weeks
  { color: 'orange', value: 2592000}, # 1 month
  { color: 'red', value: 15552000}, # 6 months
];

local configureColumn(
  old_name,
  new_name,
  unit=null,
  min=null,
  max=null,
  decimals=null,
  columnWidth=null,
  thresholds=null,
  type=null,
) =
  g.panel.table.fieldOverride.byName.new(old_name)
  + g.panel.table.fieldOverride.byName.withPropertiesFromOptions(
    g.panel.table.standardOptions.withDisplayName(new_name)
    + g.panel.table.standardOptions.withUnit(unit)
    + g.panel.table.standardOptions.withMin(min)
    + g.panel.table.standardOptions.withMax(max)
    + g.panel.table.standardOptions.withDecimals(decimals)
  )
  + g.panel.table.fieldOverride.byName.withProperty("custom.width", columnWidth)
  + g.panel.table.fieldOverride.byName.withProperty("thresholds", {"steps": thresholds})
  + (
    if type=='color-text' then
      g.panel.table.fieldOverride.byName.withProperty("custom.cellOptions", {"type": "color-text", "color": "value"})
    else {}
  )
  + (
    if type=='gauge' then
      g.panel.table.fieldOverride.byName.withProperty("custom.cellOptions", {"type": "gauge", "mode": "lcd", "valueDisplayMode": "color"}
  )    else {}
  );

local alignText(
  regexp,
  alignment
) = g.panel.table.fieldOverride.byRegexp.new(regexp)
    + g.panel.table.fieldOverride.byRegexp.withProperty("custom.align", alignment);

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
    prometheus.addQuery(
      'prometheus',
      'af_home_dir_util{namespace=~"$namespace",job="af-pod-monitor",username!=""}',
      refId='storageUtil', format='table', instant=true
    ),
    prometheus.addQuery(
      'prometheus',
      'time() - af_home_dir_last_accessed{namespace=~"$namespace", job="af-pod-monitor",username!=""}',
      refId='storageLastAccess', format='table', instant=true
    ),
    prometheus.addQuery(
      'prometheus',
      |||
        label_replace(
          time() - kube_pod_created{namespace=~"$namespace",pod=~"purdue-af-.*"},
          "userId", "$1", "pod", "purdue-af-(.*)"
        )
      |||,
      refId='podAge', format='table', instant=true
    ),
    prometheus.addQuery(
      'prometheus',
      'kube_pod_container_resource_requests{namespace=~"$namespace",pod=~"purdue-af-.*",resource="cpu"}',
      refId='cpuRequest', format='table', instant=true
    ),
    prometheus.addQuery(
      'prometheus',
      'kube_pod_container_resource_requests{namespace=~"$namespace",pod=~"purdue-af-.*",resource="nvidia_com_mig_1g_5gb"}',
      refId='gpuRequest', format='table', instant=true
    ),
    prometheus.addQuery(
      'prometheus',
      'kube_pod_container_resource_requests{namespace=~"$namespace",pod=~"purdue-af-.*",resource="memory"}',
      refId='memRequest', format='table', instant=true
    ),
    prometheus.addQuery(
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
      refId='podCpuUtilCurrent', format='table', instant=true
    ),
    prometheus.addQuery(
      'prometheus-rancher',
      |||
        sum by (pod)(
            container_memory_working_set_bytes{namespace=~"$namespace", pod=~"purdue-af-.*", container="notebook"}
        ) /
        sum by (pod)(
            kube_pod_container_resource_requests{namespace=~"$namespace", pod=~"purdue-af-.*", resource="memory", container="notebook"}
        )
      |||,
      refId='podMemUtilCurrent', format='table', instant=true
    ),
    prometheus.addQuery(
      'prometheus-rancher',
      |||
        sum by (pod) (DCGM_FI_PROF_GR_ENGINE_ACTIVE{kubernetes_node=~"geddes-g000|geddes-g001",pod=~"purdue-af-.*"})
      |||,
      refId='gpuUtilCurrent', format='table', instant=true
    ),
    prometheus.addQuery(
      'prometheus-rancher',
      |||
        sum by (pod) (
          ( DCGM_FI_DEV_FB_USED{kubernetes_node=~"geddes-g000|geddes-g001",pod=~"purdue-af-.*"}
            / ( 
              DCGM_FI_DEV_FB_USED{kubernetes_node=~"geddes-g000|geddes-g001",pod=~"purdue-af-.*"} +
              DCGM_FI_DEV_FB_FREE{kubernetes_node=~"geddes-g000|geddes-g001",pod=~"purdue-af-.*"}
            )
          )
        )
      |||,
      refId='gpuMemUtilCurrent', format='table', instant=true
    ),
    prometheus.addQuery(
      'prometheus',
      |||
        sum by (pod) (dask_scheduler_workers{job="af-pod-monitor",namespace=~"$namespace",pod=~"purdue-af-.*",})
      |||,
      refId='daskWorkers', format='table', instant=true
    ),
  ])
  + g.panel.table.queryOptions.withTransformations([
    // Transformation 1: join into a single table
    g.panel.table.transformation.withId('joinByField')
    + g.panel.table.transformation.withOptions({"byField":"pod"}),
    // Transformation 2: filter columns
    g.panel.table.transformation.withId('filterFieldsByName')
    + g.panel.table.transformation.withOptions(
        {
            "include": {"pattern": "^(Value.*|userId|username 1|node 1|docker_image_tag 1)$"},
            "exclude": {"pattern": "^(pod|.*podStatus|.*#userId|.*gpuRequest)$"}
        }
    ),
    // Transformation 3: set order of columns
    g.panel.table.transformation.withId('organize')
    + g.panel.table.transformation.withOptions(
        {
            "indexByName": {
                "userId": 0,
                "username 1": 1,
                "docker_image_tag 1": 2,
                "Value #podAge": 3,
                "Value #storageLastAccess": 4,
                "Value #podCpuUtilCurrent": 5,
                "Value #podMemUtilCurrent": 6,
                "Value #gpuUtilCurrent": 7,
                "Value #gpuMemUtilCurrent": 8,
                "Value #storageUtil": 9,
                "Value #daskWorkers": 10,
                "Value #cpuRequest": 11,
                "Value #memRequest": 12,
                "node 1": 13,
                },
        }
    ),
    // Transformation 4: convert user ID to int
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
  // Configure column  styles
  + g.panel.table.standardOptions.withOverrides([
    configureColumn("userId", "ID", columnWidth=40),
    configureColumn("pod", "Pod", columnWidth=120),
    configureColumn("node", "Node", columnWidth=120),
    configureColumn("docker_image_tag", "Version", columnWidth=70),
    configureColumn("username", "Username", columnWidth=120),
    g.panel.table.fieldOverride.byName.new("username")
    + g.panel.table.fieldOverride.byName.withProperty(
      "links",
     [{
        "title": "",
        "url": "https://cms.geddes.rcac.purdue.edu/grafana/d/single-user-stat-dashboard/single-user-statistics?&var-user=purdue-af-${__data.fields.ID}",
        "targetBlank": true
      }]
    ),
    configureColumn("Value #daskWorkers", "Dask workers", columnWidth=110),
    configureColumn("Value #podAge", "Pod age", "s", columnWidth=100, type='color-text', thresholds=ageThresholds),
    configureColumn("Value #storageLastAccess", "Last active", "s", columnWidth=100, type='color-text', thresholds=ageThresholds),
    configureColumn("Value #storageUtil", "Storage util.", "percentunit", 0, 1, 1, columnWidth=130, type='gauge', thresholds=utilizationThresholds),
    configureColumn("Value #podCpuUtilCurrent", "Pod CPU util.", "percentunit", 0, 1, 1, columnWidth=130, type='gauge', thresholds=utilizationThresholds),
    configureColumn("Value #podMemUtilCurrent", "Pod memory util.", "percentunit", 0, 1, 1, columnWidth=130, type='gauge', thresholds=utilizationThresholds),
    configureColumn("Value #gpuUtilCurrent", "GPU engine util.", "percentunit", 0, 1, 1, columnWidth=130, type='gauge', thresholds=utilizationThresholds),
    configureColumn("Value #gpuMemUtilCurrent", "GPU mem. util.", "percentunit", 0, 1, 1, columnWidth=130, type='gauge', thresholds=utilizationThresholds),
    configureColumn("Value #cpuRequest", "CPU req.", columnWidth=80),
    configureColumn("Value #gpuRequest", "GPU req.", columnWidth=80),
    configureColumn("Value #memRequest", "Memory req.", "bytes", columnWidth=110),
    // Align text in columns
    alignText(".*", "left"),
    alignText(".*request", "center"),
    ]
  )
  // Sort table
  + g.panel.table.options.withSortBy([{"displayName": "Pod age", "desc": true}])
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
+ g.dashboard.graphTooltip.withSharedCrosshair()
+ g.dashboard.withPanels([
  userTable + w(24) + h(24),
])
