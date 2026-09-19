# Purdue Slurm client configs

Per-cluster Slurm *client* trees consumed by:

- the AF Jupyter image (`docker/purdue-af`) — Hammer
- the interLink Slurm sidecar (`docker/interlink-slurm-plugin`) — every tree
- dask-gateway Slurm images (`docker/dask-gateway-server`) — Hammer

## Layout

```
slurm/
  client-versions                    # cluster -> client RPM version map
  slurm-<version>-1.el8.x86_64.rpm   # one RPM per distinct controller version
  slurm-configs-<cluster>/
    slurm.conf                       # required
    cgroup.conf                      # optional
    gres.conf                        # optional
    slist                            # optional; Hammer RCAC helper shipped into PATH
```

Which client version each cluster uses: [`client-versions`](client-versions).

`<cluster>` must match `apps/interlink/<cluster>/`, `munge-key-<cluster>`, and
`SLURM_CLUSTER=<cluster>`.

## What belongs here

Copy from `/etc/slurm` on a login node **only** submit-client material:

| Keep | What it is |
| --- | --- |
| `slurm.conf` | Required — `sbatch` / `squeue` find the controller |
| `cgroup.conf`, `gres.conf` | Harmless; some client tools expect them |
| `slist` | Hammer-only user helper (`run-as-root.sh` installs it on `PATH`) |

Do **not** commit compute/controller-only material: `prolog.d/`, `epilog.d/`,
`slurm-task-prolog`, `job_submit.lua`, `topology.conf`, `dump_parameters.lua`,
backup `*.old` / `*.new` copies, etc.

## Adding a cluster

1. Drop the client files above into `slurm/slurm-configs-<name>/`.
2. Add `<name> <version>` to `client-versions`. If that version is new, add
   `slurm-<version>-1.el8.x86_64.rpm` (from the cluster's login node).
3. Create and populate the `munge-key-<name>` PVC in `cms` (out of band, never
   in git).
4. Add `apps/interlink/<name>/`: `SLURM_CLUSTER=<name>` in the plugin env of
   `values.yaml`, and the HelmRelease postRenderer that mounts `munge-key-<name>`.
5. Merge to `main`; the plugin image rebuilds, since `slurm/` is one of its
   inputs.
