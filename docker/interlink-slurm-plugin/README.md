# interLink Slurm sidecar (Purdue)

Site image for the [interlink-slurm-plugin](https://github.com/interlink-hq/interlink-slurm-plugin)
sidecar: a thin Slurm client against RCAC Slurm on Rocky Linux 8, plus the site
Slurm client RPMs. One image serves every `apps/interlink/<cluster>/`
Deployment.

## Files

| Path | What it is |
| --- | --- |
| `PLUGIN_REF` | upstream plugin tag checked out and published as the image tag |
| `Dockerfile` | builds the plugin; extracts every client RPM and config tree from `slurm/` |
| `startup.sh` | activates `$SLURM_CLUSTER`'s config and client, loads munge, starts `munged` |

## Multi-cluster model

1. The build extracts every `slurm/slurm-*-1.el8.x86_64.rpm` into
   `/opt/purdue-af/slurm-clients/<version>/` and copies every
   `slurm/slurm-configs-<cluster>/` tree into
   `/opt/purdue-af/slurm-configs/<cluster>/`. Clusters can need different
   client versions, which one system-wide `dnf install` cannot hold.
2. At start, `startup.sh` installs the config tree named by `$SLURM_CLUSTER`,
   activates the client version [`slurm/client-versions`](../../slurm/client-versions)
   maps it to onto `/opt/purdue-af/slurm-active/bin` (first on `PATH`), loads
   `munge-key-<cluster>`, and starts `munged`.
3. A full client tree mounted at `/etc/secrets/slurm-configs` takes precedence,
   for a cluster whose configs are not in git.

## Bumping the plugin

Set the new upstream tag in `PLUGIN_REF`, the `SLURM_PLUGIN_REF` default in the
Dockerfile, and the image tag in each `apps/interlink/<cluster>/values.yaml`;
the tests fail until all agree. Publication: [RELEASING.md](../../RELEASING.md).
Adding a cluster: [slurm/README.md](../../slurm/README.md).
