---
paths:
  - "apps/**"
  - "flux/**"
---

# Flux manifests

- A chart sourced from a GitRepository needs `reconcileStrategy: Revision` on
  its HelmRelease. Flux's default `ChartVersion` re-packages only when the
  `Chart.yaml` version changes, which stays at `0.1.0` in this repository's own
  charts: template edits never reach the cluster while values edits still
  upgrade, running new values against the old chart.
  `tests/manifests/test_inrepo_charts.py` asserts it.
- A ConfigMap generator of code or scripts carries the annotation
  `kustomize.toolkit.fluxcd.io/substitute: disabled`: Flux substitution would blank
  every `${...}` in it. Values files leave it off, because they use substitution.
- Do not add a `timeout:` to a HelmRelease. A release that hangs is waiting
  for something real; find what.
- Every manifest and ConfigMap generator is listed in a `flux/*/kustomization.yaml`
  directly; no component carries a kustomization of its own. Generated
  ConfigMaps carry no name hash (`disableNameSuffixHash` in every root). A
  HelmRelease still upgrades when its `valuesFrom` ConfigMap changes, because
  Flux watches it; a Deployment mounting a generated ConfigMap does not roll.
  A long-running process that runs its code out of one compares its own
  file's content between iterations and exits 0 once it changes, so kubelet
  restarts the container on the new code (`CODE` in
  `apps/af-utils/pixi-global-sync/sync-global-env.py`); that mount takes no
  `subPath`, which kubelet never updates.
- To change a Secret: decrypt, edit, `sops -e -i` it again. The encrypted diff
  says nothing, so the commit message describes the change.
