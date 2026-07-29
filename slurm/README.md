# Purdue Slurm client configs

Per-cluster Slurm *client* trees consumed by:

- the AF Jupyter image (`docker/purdue-af`) — Hammer only today
- the interLink Slurm sidecar (`docker/interlink-slurm-plugin`) — every tree
- dask-gateway Slurm images (`docker/dask-gateway-server`) — Hammer

## Layout

```
slurm/
  slurm-<version>-1.el8.x86_64.rpm   # shared client RPM (25.11.4 for AF, interlink, dask)
  slurm-configs-<cluster>/
    slurm.conf                       # required
    cgroup.conf                      # optional
    gres.conf                        # optional
    slist                            # optional; Hammer RCAC helper shipped into PATH
```

`<cluster>` must match `apps/interlink/<cluster>/`, `munge-key-<cluster>`, and
`SLURM_CLUSTER=<cluster>`.

## What belongs here

Copy from `/etc/slurm` on a login node **only** submit-client material:

| Keep | Why |
| --- | --- |
| `slurm.conf` | Required — `sbatch` / `squeue` find the controller |
| `cgroup.conf`, `gres.conf` | Harmless; some client tools expect them |
| `slist` | Hammer-only user helper (`run-as-root.sh` installs it on `PATH`) |

Do **not** commit compute/controller-only material: `prolog.d/`, `epilog.d/`,
`slurm-task-prolog`, `job_submit.lua`, `topology.conf`, `dump_parameters.lua`,
backup `*.old` / `*.new` copies, etc.

## Adding a cluster

1. Drop the client files above into `slurm/slurm-configs-<name>/`.
2. Create and populate `munge-key-<name>` in `cms` (out of band, never in git).
3. Wire `apps/interlink/<name>/` with `SLURM_CLUSTER` + munge PVC postRenderer.
4. Merge to `main` — CI rebuilds `interlink-slurm-plugin` and retags `$PLUGIN_REF`.
