# interLink Slurm sidecar (Purdue)

Site image for the [interlink-slurm-plugin](https://github.com/interlink-hq/interlink-slurm-plugin)
sidecar. Upstream’s published image is a self-contained demo Slurm cluster;
this one is a **thin client** against RCAC Slurm on the same NVIDIA
Rocky Linux 8 CUDA base as the AF session image, plus the site Slurm RPM.

## Versioning

`PLUGIN_REF` is the single source of truth (e.g. `0.6.2-pre3`):

- Dockerfile checks out that upstream tag and defaults `ARG SLURM_PLUGIN_REF` to it
- CI publishes `ghcr.io/purdueaf/interlink-slurm-plugin:$PLUGIN_REF` (not `:latest`)
- Values pin
  `geddes-registry…/ghcr-proxy-cache/purdueaf/interlink-slurm-plugin:$PLUGIN_REF`

Bump the plugin by editing `PLUGIN_REF` (and the matching `ARG` default);
tests fail if values / Dockerfile drift.

## Multi-cluster model

One image tag is shared by every `apps/interlink/<cluster>/` Deployment:

1. Build copies every `slurm/slurm-configs-<cluster>/` tree into
   `/opt/purdue-af/slurm-configs/<cluster>/`.
2. At start, `startup.sh` installs the tree named by `$SLURM_CLUSTER` into
   `/etc/slurm`, loads `munge-key-<cluster>`, and starts `munged`.
3. Optional override: mount a full client tree at `/etc/secrets/slurm-configs`
   (takes precedence) for a cluster whose configs are not in git yet.

## Build & publish

Built by `ci-images.yml` like the other aux images:

- content-addressed `…:in-<hash>` (smoke-tested; what publish retags)
- on green `main`, publish moves `:sha-<commit>` and `:$PLUGIN_REF`
- cluster pulls via the geddes `ghcr-proxy-cache`

Input paths are in `.github/workflows/image-inputs.sh` under
`interlink-slurm-plugin` (this directory + the whole `slurm/` tree).

See [../../slurm/README.md](../../slurm/README.md) for adding a cluster.
