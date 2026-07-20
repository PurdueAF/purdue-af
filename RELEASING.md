# Releasing

The Purdue AF has **two human-minted version streams** and a set of
CI-owned continuous channels. Each stream is minted by exactly one
workflow — never create version tags by hand, and never move channel tags
by hand.

| Stream                                             | Scheme                                   | Example    | Minted by                                          | Reaches production                                                    |
| -------------------------------------------------- | ---------------------------------------- | ---------- | -------------------------------------------------- | --------------------------------------------------------------------- |
| **Platform** (everything Flux deploys)             | CalVer `YYYY.M.SEQ`                      | `2026.7.8` | **Release platform** workflow                      | immediately — production Flux tracks the newest `2026.x` tag          |
| **purdue-af image**                                | semver `0.X.Y`, repo tag `v0.X.Y`        | `v0.13.0`  | **Release image** workflow                         | at the **next platform release** (the bump commit must get tagged)    |
| **af-node-monitor image**                          | semver `0.X.Y`                           | `0.2.0`    | **Release image** workflow                         | at the next platform release                                          |
| Continuous (`:latest`, `:pre-release`, `in-`, `sha-`) | moving tags                           | —          | `ci.yml` publish stage only, behind the ci-ok gate | n/a — consumed directly by manifests / the pre-release profile        |

## When to increment

**purdue-af image** — when the content soaking as `:pre-release` should
become the default production environment. Pick the bump by user impact:

- **patch** — fixes and package additions with no behavior change;
- **minor** — environment or tooling changes users should read notes about;
- **major** — breaking changes to the user environment (removed tools,
  changed paths/defaults).

Preconditions, enforced by the workflow (bypass only with `force`):
`ci-ok` green on main HEAD, and the `:pre-release` digest identical to the
image of the current repo state. Complete the manual checklist in
[docker/purdue-af-new/README.md](docker/purdue-af-new/README.md) first.

**af-node-monitor** — when its code changed and the smoke-tested build
should ship; same bump semantics, no soak channel.

**Platform** — whenever the state of `main` (hub config, monitoring,
manifests, cronjobs, …) should reach the production namespace. This is
the **only** path to production for core components, and it is always the
second step of an image release: the values/manifest bump commit sits on
`main` until a platform tag covers it.

## How

Image release, start to finish:

1. **Actions → Release image → Run workflow** — choose the image
   (`purdue-af` or `af-node-monitor`) and the bump (or an explicit
   `version`). The workflow verifies both gates, adds the semver tag to
   the **same digest** that passed CI (never a rebuild), rewrites the
   version in the manifests (`bump-af-version.py` / `bump-aux-image.py`),
   commits to `main`, and — for purdue-af — tags `v<version>` and
   publishes a GitHub Release.
2. **Verify staging** — `cms-dev` and geddes2 roll from `main` within a
   minute; check the hub picked up the new pin.
3. **Actions → Release platform → Run workflow** — mints the next
   `YYYY.M.SEQ` tag and a GitHub Release; production Flux advances to the
   tagged commit within ~1 minute and the hub config rolls.

A platform release without an image release is just step 3.

## Rollback

- **Image**: `git revert` the release commit on `main`, then mint a new
  platform tag so production rolls back too. Old semver tags stay on ghcr
  forever; the registry GC never deletes release tags.
- **Platform**: revert the offending commits on `main` and mint a new
  platform tag (production only ever moves forward through tags).

## Rules of the road

- Channel tags (`:latest`, `:pre-release`) and build tags (`in-`, `sha-`)
  are CI-owned: they move only in the `ci.yml` publish stage, after every
  stage of the same commit is green. Hand-moving them defeats the gates.
- The `AF_RELEASE_TOKEN` secret (fine-grained PAT, `contents: write`)
  must exist — release commits/tags pushed with the default
  `GITHUB_TOKEN` do not trigger CI, so the release commit would go
  unvalidated.
- Version badges in the README read the platform tag list and
  `apps/jupyterhub/jupyterhub/values.yaml` — they update on their own;
  nothing to edit.
