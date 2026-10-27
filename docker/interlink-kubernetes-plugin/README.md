# interLink Kubernetes plugin (Purdue)

Site image for the [interlink-kubernetes-plugin](https://github.com/interlink-hq/interlink-kubernetes-plugin)
sidecar: the upstream `1.2.0` image plus four patches.

- `ensure-namespace.patch` adds an `[offloading] ensure_namespace` option.
  Upstream lists namespaces at cluster scope before every pod create;
  `ensure_namespace=False` skips that check for remote clusters whose RBAC is
  namespaced only — `apps/interlink/nrp` sets it and creates the offloading
  namespace out of band, so a missing one fails the pod create loudly instead
  of being auto-created.
- `wstunnel-client.patch` adds a `[wstunnel] client_image` option. With it
  set, a pod that carries the virtual kubelet's
  `interlink.eu/wstunnel-client-commands` annotation gets a `wstunnel-client`
  sidecar that runs that command, unprivileged, so the pod's ports reach its
  shadow pod in the local cluster. Upstream runs no tunnel client, and its full
  mesh needs privileged pods on both clusters.
- `remote-placement.patch` places and restarts the remote pod. The
  virtual kubelet sends the plugin no affinity, node selector or
  tolerations, so a pod's `interlink.eu/remote-affinity` annotation — a JSON
  core/v1 `Affinity` — becomes the remote pod's affinity, and the local
  scheduler never sees it. An `[offloading] restart_policy` option overrides
  every remote pod's restart policy.
- `remote-readiness.patch` gives the remote pod its containers' probes and
  holds the local pod's Ready until the remote readiness probe passes. The
  interLink SDK the plugin is built on drops `startupProbe`, `livenessProbe`
  and `readinessProbe`, and the virtual kubelet marks every running container
  ready, so the patch keeps the probes and reports a running, unready remote
  container as waiting. Only the first Ready waits: the virtual kubelet never
  sets a Running pod unready again.

## Files

| Path | What it is |
| --- | --- |
| `PLUGIN_REF` | upstream image tag the build extends, published as the image tag |
| `Dockerfile` | applies the patches to the source shipped in the upstream image |
| `ensure-namespace.patch` | adds `[offloading] ensure_namespace` (default on, upstream behavior) |
| `wstunnel-client.patch` | adds `[wstunnel] client_image` (default unset: no sidecar, upstream behavior) |
| `remote-placement.patch` | adds `interlink.eu/remote-affinity` and `[offloading] restart_policy` (default unset: the pod's own policy) |
| `remote-readiness.patch` | keeps the probes and reports an unready container as waiting; patches the installed interLink SDK too, so it applies from `/` |

## Bumping the plugin

Set the new upstream image tag in `PLUGIN_REF`, the `UPSTREAM_IMAGE` default in
the Dockerfile, and the image tag in each `apps/interlink/<cluster>/values.yaml`;
the tests fail until all agree — and the build fails until the patches still
apply to the new source. Publication: [RELEASING.md](../../RELEASING.md).

A change to a patch republishes the same `:<PLUGIN_REF>` tag. The node pulls
its plugin image on every start, so delete the `<nodeName>-node` pod once the
image is published to load it.
