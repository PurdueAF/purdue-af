# Image registry architecture

```
ci.yml (stage 1: content-addressed builds)
  build → smoke test → push :in-<input-hash>           ← source of truth
ci.yml (stage 3 publish: main only, behind the ci-ok gate)
  retag → ghcr.io/purdueaf/<name>:sha-<commit> (provenance)
        → the image's channel tag (RELEASING.md)
                          │
cluster pulls ◀── geddes-registry.rcac.purdue.edu/ghcr-proxy-cache/purdueaf/<name>
                  (Harbor proxy-cache, same mechanism as docker-hub-cache)
```

- **ghcr.io** is the publication registry: built by CI, authenticated with the
  built-in `GITHUB_TOKEN` (no separate account or secret), only smoke-tested
  images are pushed, every image carries `org.opencontainers.image.revision`.
- **geddes-registry** is the cluster-facing registry: manifests reference the
  `ghcr-proxy-cache` project so pulls are LAN-local and survive ghcr outages.
  The cache revalidates moving tags upstream on each pull and serves the
  last-known image when ghcr is unreachable.

Which tag each image publishes to, what pins it in the cluster, and how a
version is minted: [RELEASING.md](../RELEASING.md).

## Images built outside CI

Two images are pulled straight from the `cms/` project on geddes-registry
rather than through `ghcr-proxy-cache`. `ci.yml` does not build them, and this
repository holds neither their sources nor a build job for them: a new tag
comes from outside it.

| Image in `cms/` | Pinned by |
| --- | --- |
| `dask-gateway-server` | `apps/dask-gateway/dask-gateway-k8s-slurm` |
| `servicex-science-combined-root-coffea` | `apps/servicex/servicex`, `-test` |

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
