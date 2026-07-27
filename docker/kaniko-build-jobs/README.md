# Out-of-band image builds

Three images are too large to build on a GitHub-hosted runner, so they are
built in-cluster with kaniko instead of by `ci.yml`. Everything else — the
purdue-af image, agentic-interface, af-pod-monitor, af-node-monitor — is built
and published by CI; see [../REGISTRY.md](../REGISTRY.md).

| Job | Image | Consumed by |
| --- | --- | --- |
| `build-dask.yaml` | `dask-gateway-server:2023.9.0-purdue.v4-hammer` | `apps/dask-gateway/dask-gateway-k8s-slurm` |
| `build-interlink.yaml` | `interlink-slurm-plugin:0.6.2-pre3` | `apps/interlink/{hammer,gautschi}` |
| `build-servicex-science-coffea.yaml` | `servicex-science-combined-root-coffea:5.7.2-6.38.04` | `apps/servicex`, `-anvil`, `-test` |

These are **not** reconciled by Flux. Run one by hand, watch it, then delete it:

```bash
kubectl apply -n cms -f docker/kaniko-build-jobs/build-interlink.yaml
kubectl logs -n cms job/kaniko-build-interlink -f
kubectl delete -n cms -f docker/kaniko-build-jobs/build-interlink.yaml
```

Each job builds from `git://github.com/PurdueAF/purdue-af.git` at `main`, not
from your working tree — push first, or the build will not see your changes.
Tags are written into the job manifest, so bumping a version means editing the
`--destination` argument.
