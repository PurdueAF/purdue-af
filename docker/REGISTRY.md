# Image registry architecture

```
ci.yml (stage 1: content-addressed builds)
  build → smoke test → push :in-<input-hash>           ← source of truth
ci.yml (stage 3 publish: main only, behind the ci-ok gate)
  retag → ghcr.io/purdueaf/<name>:sha-<commit> (provenance)
        → :latest (aux continuous channel) / :pre-release (purdue-af)
                          │
cluster pulls ◀── geddes-registry.rcac.purdue.edu/ghcr-proxy-cache/purdueaf/<name>
                  (Harbor proxy-cache, same mechanism as docker-hub-cache)
```

- **ghcr.io** is the publication registry: built by CI, authenticated with the
  built-in `GITHUB_TOKEN` (no separate account or secret), only smoke-tested
  images are pushed, every image carries `org.opencontainers.image.revision`.
- **geddes-registry** stays the cluster-facing registry: manifests reference
  the `ghcr-proxy-cache` project so pulls are LAN-local and survive ghcr
  outages (cache serves last-known images).
- **Tag taxonomy**: `in-<hash>` (immutable, names the exact input-tree state;
  what CI builds and tests), `sha-<commit>` (immutable provenance, added at
  publish), `:latest` / `:pre-release` (moving channel tags, moved ONLY by the
  ci.yml publish stage after the full pipeline is green), semver (immutable,
  added only by release-image.yml, promote-by-digest).
- **CI-built images**: purdue-af, agentic-interface, af-pod-monitor,
  af-node-monitor. Other large images (dask-gateway variants,
  interlink-slurm-plugin, servicex-science-coffea) are built only by the
  in-cluster kaniko jobs (`docker/kaniko-build-jobs/`) — they exceed
  GitHub-hosted runner limits. Pixi environments are validated by the
  ci-pixi-global.yml stage instead.
- The kaniko jobs are therefore the long-term build path for the large images —
  they are not legacy and cannot be retired unless the heavy builds move to
  infrastructure with cluster-grade disk/CPU (e.g. a self-hosted runner).

## One-time setup (cluster admin)

1. **Make the ghcr packages public** (after the first main push creates them):
   GitHub → PurdueAF org → Packages → each `<name>` → Package settings →
   Change visibility → Public. (First push creates them private by default;
   public packages need no pull credentials anywhere.)
2. **Create the Harbor proxy-cache project** on geddes-registry:
   - Registries → New endpoint: provider "GitHub GHCR" (or generic
     "Docker Registry"), URL `https://ghcr.io`, no credentials (public).
   - Projects → New project: name `ghcr-cache`, enable "Proxy Cache",
     select the ghcr endpoint.
   - **Set the project's Access Level to Public** (Projects → ghcr-cache →
     Configuration → Public). Harbor projects are private by default, and
     user pods carry no geddes pull secrets — a private project 401s
     every spawn.
   - Verify from a cluster node:
     `crictl pull geddes-registry.rcac.purdue.edu/ghcr-cache/purdueaf/agentic-interface:sha-<commit>`

## Deployment references (current state)

All aux images (agentic-interface, af-pod-monitor, af-node-monitor) pull
`:latest` through the `ghcr-proxy-cache` project — the continuous
channel, moved only by the ci.yml publish stage after a fully green
pipeline. The purdue-af image is pinned by semver in
`apps/jupyterhub/jupyterhub/values.yaml` and promoted via
release-image.yml (see RELEASING.md at the repo root).

Follow-up (IMPROVEMENT_PLAN item 13): Flux ImagePolicy +
ImageUpdateAutomation can replace the `:latest` channel with reviewed
pin-bump commits once desired.
