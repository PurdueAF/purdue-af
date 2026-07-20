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
  ci-pixi.yml stage instead.
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

## Switching a deployment (checklist, per image)

Only after the proxy cache works:

1. Pick the `sha-<commit>` tag of a main build whose CI was fully green.
2. Update the manifest to
   `geddes-registry.rcac.purdue.edu/ghcr-cache/purdueaf/<name>:sha-<commit>`.
   First targets (also closes IMPROVEMENT_PLAN item 3's `:latest`s):
   - `apps/agentic-interface/deployment.yaml` (`cms/agentic-interface:latest`)
   - `apps/jupyterhub/jupyterhub/values.yaml` (`cms/af-pod-monitor:latest` ×2)
   - af-node-monitor refs (`JOB_IMAGE` default + deployment)
3. Flux reconciles; verify the pod runs, then delete the old geddes-native
   image copy at leisure.

Follow-up (IMPROVEMENT_PLAN item 13): Flux ImagePolicy on the `sha-` tag
pattern + ImageUpdateAutomation turns step 1–2 into reviewed PRs.
