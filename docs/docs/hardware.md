# Hardware

Purdue AF runs on dedicated nodes of the Purdue Geddes composable cluster,
supplemented by additional CPU and GPU nodes. User sessions and Dask Gateway
workers with the Kubernetes backend are scheduled on these nodes; Slurm jobs and
Dask Gateway workers with the Slurm backend run on the Purdue Hammer cluster.

| Node name          | Node type      | Quantity | Cores    | RAM (GB) | GPU (on each node)  | Storage (TB) |
| ------------------ | -------------- | -------- | -------- | -------- | ------------------- | ------------ |
| geddes-b0X         | Geddes CPU     | 3        | 128      | 512      |                     | 8            |
| geddes-g0X         | Geddes GPU     | 3        | 128      | 512      | 2x NVIDIA A100 40GB | 8            |
| paf-a0X            | External       | 3        | 256      | 512      | 2x NVIDIA T4 16GB   |              |
| paf-b0X            | former CMS FEs | 2        | 256      | 512      | 1x NVIDIA T4 16GB   |              |
| a337               | Gautschi CPU   | 1        | 192      | 384      |                     |              |
| Additional storage |                |          |          |          |                     | 4            |
| **TOTAL**          |                | **12**   | **2240** | **6016** | **14**              | **52 TB**    |

!!! note "See also"

    * [GPU access at Purdue AF](gpus.md)
    * [Storage volumes](storage.md)
    * Live utilization metrics: [monitoring dashboard](https://cms.geddes.rcac.purdue.edu/grafana/d/purdue-af-dashboard/purdue-analysis-facility-dashboard){ target="_blank" }
