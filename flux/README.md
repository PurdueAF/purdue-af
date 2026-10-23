# Flux roots

Each directory is one Flux Kustomization: a `kustomization.yaml` listing, file
by file, the manifests under `apps/` it applies; a `git-repository.yaml` naming
the git ref it applies them from; and a `flux-kustomization.yaml` carrying the
`postBuild` substitutions for that environment and the SOPS decryption secret.

| Root               | Applies from                                | Namespace | Holds                                                             |
| ------------------ | ------------------------------------------- | --------- | ----------------------------------------------------------------- |
| `core-production/` | newest platform tag (`semver: 2026.x`)      | `cms`     | the core components on the production cluster                     |
| `core-geddes2/`    | branch `main`                               | `cms`     | its own list of core components for the `geddes2` cluster (`cmsdev.` hostname), not production's |
| `experimental/`    | branch `main-validated`, advanced by CI     | `cms`     | the experimental components on the production cluster             |

`enable-sops.sh` creates the `sops-age` Secret each root decrypts with;
`flux-secret.yaml` is the GitHub token template the GitRepository sources
read with. Both are applied out of band, once.

A manifest reaches a cluster only once it is listed in a root here. What each
ref means for a change, and how to roll one back: [RELEASING.md](../RELEASING.md).
