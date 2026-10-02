---
paths:
  - "apps/**"
  - "deploy/**"
---

# Flux manifests

- A chart that lives in this repository needs `reconcileStrategy: Revision` on
  its HelmRelease. `Chart.yaml` stays at `0.1.0`, and Flux's default
  `ChartVersion` re-packages only when that string changes: template edits
  never reach the cluster while values edits still upgrade, running new values
  against the old chart. `tests/manifests/test_inrepo_charts.py` asserts it.
- Do not add a `timeout:` to a HelmRelease. A release that hangs is waiting
  for something real; find what.
- Every manifest and ConfigMap generator is listed in a `deploy/*/kustomization.yaml`
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
