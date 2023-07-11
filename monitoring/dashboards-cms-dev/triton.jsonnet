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
  template.new(
    'hub',
    datasource='$PROMETHEUS_DS',
    query='label_values(kube_service_labels{service="hub"}, namespace)',
    // Allow viewing dashboard for multiple combined hubs
    includeAll=true,
    multi=true
  ),
];


// Hub usage stats
local tritonInferenceCount = graphPanel.new(
  'Inferences per second (standalone Triton servers)',
  decimals=0,
  stack=false,
  min=0,
  datasource='$PROMETHEUS_DS'
).addTargets([
  prometheus.target(
    |||
      rate(
            (
                sum(nv_inference_count) by (model)
            )[1m:1s]
        )
    |||,
    legendFormat='{{model}}',
  ),
]);




dashboard.new(
  'Triton Dashboard',
  tags=['triton'],
  uid='triton-dashboard',
  editable=true
).addTemplates(
  templates
).addPanel(
  row.new('Triton stats'), {}
).addPanel(
  tritonInferenceCount, {}
)
