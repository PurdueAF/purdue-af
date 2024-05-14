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

g.dashboard.new('SONIC Dashboard')
+ g.dashboard.withUid('sonic-dashboard')
+ g.dashboard.withDescription('SONIC monitoring')
// + g.dashboard.withLiveNow()
+ g.dashboard.withRefresh('1m')
// + g.dashboard.withStyle(value="dark")
+ g.dashboard.withTimezone(value="browser")
+ g.dashboard.time.withFrom(value="now-6h")
+ g.dashboard.time.withTo(value="now")
+ g.dashboard.graphTooltip.withSharedCrosshair()
+ g.dashboard.withPanels([

  // myPanels.row.triton_metrics             + w(24) + h(2),
  myPanels.stat.deployedTritonLB          + w(3)  + h(4),
  myPanels.stat.deployedTritonServers     + w(3)  + h(4),
  myPanels.timeSeries.tritonNumServers    + w(9) + h(10),
  myPanels.timeSeries.tritonMemUtil       + w(9) + h(10),

  myPanels.timeSeries.tritonGPUload       + w(12) + h(10),
  myPanels.timeSeries.tritonServerSaturation + w(12) + h(10),

  myPanels.timeSeries.tritonQueueTimeByModel  + w(12) + h(10),
  myPanels.timeSeries.tritonQueueTimeByServer  + w(12) + h(10),

  myPanels.timeSeries.tritonInferenceCount  + w(12) + h(10),
  myPanels.timeSeries.tritonInferencesPerLB  + w(12) + h(10),

  myPanels.timeSeries.envoyLatency  + w(8) + h(10),
  myPanels.timeSeries.tritonLatency  + w(8) + h(10),
  myPanels.timeSeries.envoyOverhead  + w(8) + h(10),
  myPanels.timeSeries.envoyClients  + w(8) + h(10),
  myPanels.timeSeries.envoyMemUtil  + w(8) + h(10),
  myPanels.timeSeries.envoyCpuUtil  + w(8) + h(10),

])