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
  cluster admin out-of-band — they exceed
  GitHub-hosted runner limits. Pixi environments are validated by the
  ci-pixi-global.yml stage instead.

## Registry configuration

- The ghcr packages are **public**, so nothing in the cluster needs pull
  credentials for them.
- `ghcr-proxy-cache` is a Harbor **proxy-cache** project on geddes-registry
  pointing at `https://ghcr.io` (no credentials), with its access level set
  to **Public** — user pods carry no geddes pull secrets, so a private
  project would 401 on every spawn.

Verify from a cluster node:

```
crictl pull geddes-registry.rcac.purdue.edu/ghcr-proxy-cache/purdueaf/agentic-interface:latest
```

## What pulls what

All aux images (agentic-interface, af-pod-monitor, af-node-monitor) pull
`:latest` through the `ghcr-proxy-cache` project — the continuous
channel, moved only by the ci.yml publish stage after a fully green
pipeline. The purdue-af image is pinned by semver in
`apps/jupyterhub/jupyterhub/values.yaml` and promoted via
release-image.yml (see RELEASING.md at the repo root).
