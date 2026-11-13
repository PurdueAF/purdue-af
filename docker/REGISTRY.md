# Image registry architecture

```
ci.yml (stage 1: content-addressed builds)
  build → smoke test → push :in-<input-hash>           ← source of truth
ci.yml (stage 3 publish: main only, behind the ci-ok gate)
  retag → ghcr.io/purdueaf/<name>:sha-<commit> (provenance)
        → the image's channel tag (RELEASING.md)
                          │
cluster pulls ◀── geddes2-registry.rcac.purdue.edu/ghcr-proxy-cache/purdueaf/<name>
                  (Harbor proxy-cache)
NRP pulls     ◀── gitlab-registry.nrp-nautilus.io/kondratyevd/purdueaf/ray-nrp
                  (copied by ci.yml's publish stage)
```

- **ghcr.io** is the publication registry: built by CI, authenticated with the
  built-in `GITHUB_TOKEN` (no separate account or secret), only smoke-tested
  images are pushed, every image carries `org.opencontainers.image.revision`.
- **NRP's GitLab registry** holds `ray-nrp:latest` for the pods of Ray jobs on
  NRP, close to them and independent of ghcr: the publish stage copies
  ghcr's `:latest` there, by digest, whenever the two differ, with the
  `NRP_REGISTRY_USER` and `NRP_REGISTRY_TOKEN` secrets (a GitLab access token
  with `write_registry`). Without them, or when NRP's registry fails, the
  step warns and the deploy goes on.
- **geddes2-registry** is the cluster-facing registry: manifests reference the
  `ghcr-proxy-cache` project so pulls are LAN-local and survive ghcr outages.
  The cache revalidates moving tags upstream on each pull and serves the
  last-known image when ghcr is unreachable.

Which tag each image publishes to, what pins it in the cluster, and how a
version is minted: [RELEASING.md](../RELEASING.md).

## Images built outside CI

These images are pulled straight from the `cms/` project on geddes2-registry
rather than through `ghcr-proxy-cache`. `ci.yml` does not build them, and this
repository holds neither their sources nor a build job for them: a new tag
comes from outside it.

| Image in `cms/` | Used by |
| --- | --- |
| `cmsaf-base-notebook` | `apps/jupyterhub/jupyterhub` |
| `servicex_app` | `apps/servicex/servicex-eos` |
| `servicex-transformer-sidecar` | `apps/servicex/servicex-eos` |

## Registry configuration

- The ghcr packages are **public**, so nothing in the cluster needs pull
  credentials for them.
- `ghcr-proxy-cache` is a Harbor **proxy-cache** project on geddes2-registry
  pointing at `https://ghcr.io` (no credentials), with its access level set
  to **Public** — user pods carry no geddes pull secrets, so a private
  project would 401 on every spawn.
- `dockerhub-proxy-cache` is the same kind of project pointing at Docker Hub.
- The GitLab project `kondratyevd/purdueaf` on `gitlab.nrp-nautilus.io` is
  **public**, so NRP's pods pull from it with no pull secret.

Verify from a cluster node:

```
crictl pull geddes2-registry.rcac.purdue.edu/ghcr-proxy-cache/purdueaf/agentic-interface:latest
```
