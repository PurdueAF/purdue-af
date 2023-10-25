local g = import 'github.com/grafana/grafonnet/gen/grafonnet-latest/main.libsonnet';

{
    af_metrics:: g.panel.row.new('Purdue Analysis Facility metrics'),
    gpu_metrics:: g.panel.row.new('GPU metrics'),
    triton_metrics:: g.panel.row.new('Triton metrics'),
    dask_metrics:: g.panel.row.new('Dask metrics'),
    slurm_metrics:: g.panel.row.new('Slurm metrics'),
    hub_metrics:: g.panel.row.new('JupyterHub diagnostocs'),
}