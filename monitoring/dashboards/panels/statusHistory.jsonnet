local prometheus = import 'prometheus.libsonnet';
local panels = import 'panels.libsonnet';

{
  eosMount:: panels.statusHistory(
    "EOS mount",
    targets=[
      prometheus.addQuery(
        'prometheus',
        'sum by (node) (af_node_mount_valid{mount_name="eos"})',
        legendFormat='{{ node }}', interval='10m'
      ),
    ],
    showValue='never',
    hideLegend=true,
    thresholdSteps=[
      { color: 'red', value: 0 },
      { color: 'green', value: 1 },
    ]
  ),

  depotMount:: panels.statusHistory(
    "Depot mount",
    targets=[
      prometheus.addQuery(
        'prometheus',
        'sum by (node) (af_node_mount_valid{mount_name="/depot/"})',
        legendFormat='{{ node }}', interval='10m'
      ),
    ],
    showValue='never',
    hideLegend=true,
    thresholdSteps=[
      { color: 'red', value: 0 },
      { color: 'green', value: 1 },
    ]
  ),

  workMount:: panels.statusHistory(
    "/work/ mount",
    targets=[
      prometheus.addQuery(
        'prometheus',
        'sum by (node) (af_node_mount_valid{mount_name="/work/"})',
        legendFormat='{{ node }}', interval='10m'
      ),
    ],
    showValue='never',
    hideLegend=true,
    thresholdSteps=[
      { color: 'red', value: 0 },
      { color: 'green', value: 1 },
    ]
  ),

  cvmfsMount:: panels.statusHistory(
    "CVMFS mount",
    targets=[
      prometheus.addQuery(
        'prometheus',
        'sum by (node) (af_node_mount_valid{mount_name="cvmfs"})',
        legendFormat='{{ node }}', interval='10m'
      ),
    ],
    showValue='never',
    hideLegend=true,
    thresholdSteps=[
      { color: 'red', value: 0 },
      { color: 'green', value: 1 },
    ]
  ),

}
