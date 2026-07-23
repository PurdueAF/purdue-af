# pixi-global-sync — keeps `/work/pixi/global` in sync with the repo

Single-replica Deployment (running the production AF image) that does
exactly what an admin does by hand whenever
[`pixi/global/pixi.toml`](../../../pixi/global/pixi.toml) /
[`pixi.lock`](../../../pixi/global/pixi.lock) change on `main`: update the
two manifest files, run `pixi install --locked`, confirm the env imports.
Nothing else. `/work/pixi/global` stays a **plain pixi project directory**
— no symlinks, no build copies, no versioned dirs. The only addition on
the share is the persistent package cache at `/work/pixi/.cache`.

## Why this shape

- **Validation happens upstream, once — and the delivery channel is the
  gate.** `ci-pixi-global.yml` solves and import-smokes every lock change
  inside the AF image, and the manifests reach the daemon via the Flux
  `experimental` channel, which tracks the `main-validated` branch —
  advanced only by the ci.yml publish stage behind `ci-ok`. Content that
  did not pass the full pipeline structurally cannot arrive here. The daemon re-verifies
  after each install (and every 6 h), so a broken-on-disk env alerts and
  self-heals, but it doesn't duplicate CI's staging pipeline on `/work`.
- **In-place updates are short and boring.** With the cache on the same
  filesystem, `pixi install --locked` is mostly hardlink swaps. Running
  kernels keep already-imported modules; as with any env update (manual
  ones included), lazily-imported packages can mix until the kernel
  restarts. A symlink/blue-green scheme was considered and rejected:
  CPython doesn't realpath `sys.path`, so flips silently mix envs inside
  running kernels anyway — all cost, no benefit (verified empirically).
- **No state files.** Drift = byte-difference between the mounted desired
  manifests (ConfigMap, kubelet-refreshed ~1 min after Flux applies) and
  the live ones. Change latency merge→live ≈ Flux interval + 1 min +
  install time.

## Manual work / escape hatch

The env is repo-owned: hand-edits in `/work/pixi/global` are treated as
drift and reconciled back within ~1 min. For hands-on experimentation:

    touch /work/pixi/global/.sync-pause    # daemon goes hands-off (metric: paused=1)
    ...edit pixi.toml, pixi install, test...
    rm /work/pixi/global/.sync-pause       # daemon reconciles back to repo state

Upstream the change via a normal PR to `pixi/global/` when done — CI
validates the lock and the daemon applies it everywhere.

## Observability

`/metrics` on :9099 (Service labeled `scrape-metrics`, plus a direct
prometheus-server scrape job — the ServiceMonitor path feeds Rancher's
Prometheus, not the one holding our alerts). Alerts: out-of-sync too
long, env unhealthy, metrics absent. Note: a forgotten `.sync-pause`
eventually surfaces as AFGlobalEnvOutOfSync.

## Runbook

- **Roll back an env change:** revert the lock commit on `main`; the
  daemon reconciles back (warm cache ⇒ minutes).
- **Watch a sync:** `kubectl logs deploy/pixi-global-sync -f`
- **Force a re-sync now:** `rm /work/pixi/global/pixi.lock` (instant
  drift) or wait ≤60 s after Flux applies a change.

Deployed via `deploy/experimental/` for the trial period; promote by
moving the resource + configMapGenerator entries to
`deploy/core-production/`.
