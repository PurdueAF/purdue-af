# Flux roots

Each directory is one Flux Kustomization: a `kustomization.yaml` listing, file
by file, the manifests under `apps/` it applies; a `git-repository.yaml` naming
the git ref it applies them from; and a `flux-kustomization.yaml` carrying the
`postBuild` substitutions for that environment and the SOPS decryption secret,
where its manifests use them.

| Root               | Applies from                                | Namespace | Holds                                                             |
| ------------------ | ------------------------------------------- | --------- | ----------------------------------------------------------------- |
| `core-production/` | newest platform tag                         | `cms`     | the core components on the production cluster                     |
| `core-geddes2/`    | branch `main`                               | `cms`     | its own list of core components for the `geddes2` cluster (`cmsdev.` hostname), not production's |
| `experimental/`    | branch `main-validated`, advanced by CI     | `cms`     | the experimental components on the production cluster             |
| `af-image/`        | newest purdue-af image tag `vX.Y.Z`         | `cms`     | pixi-global-sync, so `/work/pixi/global` changes only with an image release |

`enable-sops.sh` creates the `sops-age` Secret each root decrypts with;
`flux-secret.yaml` is the GitHub token template the GitRepository sources
read with. Those two and each root's `git-repository.yaml` and
`flux-kustomization.yaml` are applied out of band: a merged edit to either, a new
`postBuild` variable included, takes effect only after applying it by hand.

```bash
kubectl -n cms apply -f flux/<root>/git-repository.yaml -f flux/<root>/flux-kustomization.yaml
```

Three more things exist only on the cluster or its storage: the PV that
production's `hub-db-dir` claim selects by label, the check files the storage
probes read (`CHECK_FILE` and `FIO_FILE` in
`apps/monitoring/af-monitoring/daemonset-af-node-probe.yaml`), and
`grid-certs-secret` ([its recipe](../apps/jupyterhub/af-x509-secrets/README.md)).

A manifest reaches a cluster only once it is listed in a root here. What each
ref means for a change, and how to roll one back: [RELEASING.md](../RELEASING.md).
