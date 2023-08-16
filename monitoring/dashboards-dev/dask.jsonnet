#!/usr/bin/env jsonnet -J ../vendor
// Deploys one dashboard - "JupyterHub dashboard",
// with useful stats about usage & diagnostics.
local grafana = import 'grafonnet/grafana.libsonnet';
local dashboard = grafana.dashboard;
local singlestat = grafana.singlestat;
local graphPanel = grafana.graphPanel;
local prometheus = grafana.prometheus;
local template = grafana.template;
local tablePanel = grafana.tablePanel;
local row = grafana.row;
local heatmapPanel = grafana.heatmapPanel;

local jupyterhub = import 'jupyterhub.libsonnet';
local standardDims = jupyterhub.standardDims;

local templates = [
  template.datasource(
    name='PROMETHEUS_DS',
    query='prometheus',
    current=null,
    hide='label',
  ),
  // template.new(
  //   'hub',
  //   datasource='$PROMETHEUS_DS',
  //   // query='label_values(kube_service_labels{service="hub"}, namespace)',
  //   // Allow viewing dashboard for multiple combined hubs
  //   // includeAll=true,
  //   // multi=true
  // ),
];


local daskSlurmSchedulers = graphPanel.new(
  'Number of active Dask SLURM schedulers',
  decimals=0,
  stack=false,
  min=0,
  datasource='$PROMETHEUS_DS'
).addTargets([
  prometheus.target(
    |||
      count(dask_scheduler_workers)/4 or vector(0)
    |||,
    legendFormat='Number of schedulers',
  ),
]);

local daskSlurmWorkers = graphPanel.new(
  'Number of Dask SLURM workers created on Hammer',
  decimals=0,
  stack=false,
  min=0,
  datasource='$PROMETHEUS_DS'
).addTargets([
  prometheus.target(
    |||
      sum(dask_scheduler_workers) or vector(0)
    |||,
    legendFormat='Number of workers',
  ),
]);



dashboard.new(
  'Dask Dashboard',
  tags=['dask'],
  uid='dask-dashboard',
  editable=true
).addTemplates(
  templates
).addPanel(
  row.new('Dask stats'), {}
).addPanel(
  daskSlurmSchedulers, {}
).addPanel(
  daskSlurmWorkers, {}
)
