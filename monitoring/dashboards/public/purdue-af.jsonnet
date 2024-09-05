local g = import 'github.com/grafana/grafonnet/gen/grafonnet-latest/main.libsonnet';

local myPanels = {
  row: import 'row.jsonnet',
  timeSeries: import 'timeSeries.jsonnet',
  stat: import 'stat.jsonnet',
  table: import 'table.jsonnet',
  gauge: import 'gauge.jsonnet',
  barGauge: import 'barGauge.jsonnet',
  heatmap: import 'heatmap.jsonnet',
  statusHistory: import 'statusHistory.jsonnet',
  placeholder: import 'placeholder.jsonnet'
};

local w = g.panel.timeSeries.gridPos.withW;
local h = g.panel.timeSeries.gridPos.withH;

g.dashboard.new('Purdue Analysis Facility Dashboard')
+ g.dashboard.withUid('purdue-af-dashboard')
+ g.dashboard.withDescription('Purdue Analysis Facility monitoring')
// + g.dashboard.withLiveNow()
+ g.dashboard.withRefresh('1m')
// + g.dashboard.withStyle(value="dark")
+ g.dashboard.withTimezone(value="browser")
+ g.dashboard.time.withFrom(value="now-6h")
+ g.dashboard.time.withTo(value="now")
+ g.dashboard.graphTooltip.withSharedCrosshair()
+ g.dashboard.withPanels([
  myPanels.row.af_metrics               + w(24) + h(2),
// 
  myPanels.stat.totalRunningPods        + w(4)  + h(4),
  myPanels.timeSeries.usersPerNode      + w(8)  + h(8),
  myPanels.barGauge.nodeCpuReqQuotaBarGauge + w(6) + h(7),
  // myPanels.timeSeries.nodeCpuRequest      + w(6) + h(7),
  // myPanels.barGauge.nodeCpuUtilBarGauge + w(6) + h(7),
  myPanels.timeSeries.nodeCpuUtil       + w(6) + h(7),


// 
  myPanels.stat.totalRegisteredUsers    + w(4)  + h(4),
  myPanels.placeholder.placeholder_tr   + w(8) + h(0),
  myPanels.barGauge.nodeMemReqQuotaBarGauge + w(6) + h(7),
  // myPanels.timeSeries.nodeMemoryRequest   + w(6) + h(7),
  // myPanels.barGauge.nodeMemUtilBarGauge + w(6) + h(7),
  myPanels.timeSeries.nodeMemoryUtil    + w(6) + h(7),


// 
  myPanels.gauge.nPodsQuota              + w(4)  + h(6),
  myPanels.gauge.cpuQuota                + w(4)  + h(6),
  myPanels.gauge.memQuota                + w(4)  + h(6),
  myPanels.placeholder.placeholder_tr   + w(6) + h(0),
// 
  myPanels.row.storage               + w(24) + h(2),
  myPanels.statusHistory.depotMount           + w(6) + h(6),
  myPanels.statusHistory.workMount            + w(6) + h(6),
  myPanels.statusHistory.eosMount          + w(6) + h(6),
  myPanels.statusHistory.cvmfsMount           + w(6) + h(6),

// 
  myPanels.placeholder.placeholder_tr   + w(8) + h(0.1),
  // myPanels.timeSeries.nodeEphStorageUsage + w(8) + h(8),
  // myPanels.placeholder.placeholder_tr   + w(40) + h(0.1),

  myPanels.row.gpu_metrics               + w(24) + h(2),
  myPanels.gauge.gpuTemp                + w(8)  + h(10),
  myPanels.timeSeries.gpuPower          + w(9)  + h(10),
  myPanels.table.gpuSlices              + w(7)  + h(10),

  myPanels.timeSeries.nGPUreq          + w(8)  + h(10),
  myPanels.timeSeries.gpuGrEngineUtil   + w(8) + h(10),
  // myPanels.placeholder.placeholder_tr   + w(12) + h(0),
  // myPanels.placeholder.placeholder_tr   + w(3) + h(0),
  myPanels.timeSeries.gpuMemUtil        + w(8) + h(10),
  // myPanels.placeholder.placeholder_tr   + w(20) + h(0.5),

  myPanels.row.slurm_metrics                 + w(24) + h(2),
  myPanels.timeSeries.hammerSlurmJobs        + w(12) + h(10),

  myPanels.row.dask_metrics                 + w(24) + h(2),
  myPanels.timeSeries.daskSchedulers       + w(8) + h(8),
  myPanels.timeSeries.daskWorkers          + w(8) + h(8),
  myPanels.timeSeries.daskClients         + w(8) + h(8),
  myPanels.placeholder.placeholder_tr   + w(20) + h(0.5),

  myPanels.row.hub_metrics               + w(24) + h(2),
  myPanels.timeSeries.hubResponseCodes       + w(8) + h(10),
  myPanels.timeSeries.hubResponseLatency     + w(8) + h(10),
  myPanels.timeSeries.serverStartTimes       + w(8) + h(10),
  myPanels.timeSeries.agcEventRate           + w(10) + h(10),

  // myPanels.timeSeries.nodeCpuStats  + w(10) + h(10),


])