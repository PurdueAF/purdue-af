# interLink Kubernetes plugin (Purdue)

Site image for the [interlink-kubernetes-plugin](https://github.com/interlink-hq/interlink-kubernetes-plugin)
sidecar: the upstream `1.2.0` image plus `ensure-namespace.patch`, which adds an
`[offloading] ensure_namespace` option. Upstream lists namespaces at cluster
scope before every pod create; `ensure_namespace=False` skips that check for
remote clusters whose RBAC is namespaced only — `apps/interlink/nrp` sets it
and creates the offloading namespace out of band, so a missing one fails the
pod create loudly instead of being auto-created.

## Files

| Path | What it is |
| --- | --- |
| `PLUGIN_REF` | upstream image tag the build extends, published as the image tag |
| `Dockerfile` | applies the patch to the source shipped in the upstream image |
| `ensure-namespace.patch` | adds `[offloading] ensure_namespace` (default on, upstream behavior) |

## Bumping the plugin

Set the new upstream image tag in `PLUGIN_REF`, the `UPSTREAM_IMAGE` default in
the Dockerfile, and the image tag in each `apps/interlink/<cluster>/values.yaml`;
the tests fail until all agree — and the build fails until the patch still
applies to the new source. Publication: [RELEASING.md](../../RELEASING.md).
