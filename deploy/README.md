## Continuous Deployment

The components of the Analysis Facility are divided into **core** and **experimental**:

- **Core**: stable and reliable services
- **Experimental**: subject to rapid prototyping

### Deployment strategy

- Core components are deployed into the production namespace `cms` from
  the newest platform CalVer tag (`YYYY.M.SEQ`).
- Experimental components are deployed from the CI-owned branch
  `ci/main-passed` (near tip of `main`, only advanced after the full
  pipeline is green) into the production namespace. Inspect the tip in
  the GitHub UI: https://github.com/PurdueAF/purdue-af/tree/ci/main-passed

### Update process

- To update a core component, push to `main` — the CI pipeline validates
  the full state (the `ci-ok` gate). It reaches production when the next
  platform tag is minted — see [RELEASING.md](../RELEASING.md) for when
  and how versions are incremented (platform tags and image versions are
  minted by the release workflows, never by hand).
- To update an experimental component, push to `main` — after `ci-ok`
  succeeds, the publish stage force-pushes `ci/main-passed` to that
  commit and Flux deploys it (experimental components are purposely
  brittle for faster prototyping, but still behind the same CI gate as
  image channels). Do not push to `ci/main-passed` by hand — the
  `protect-ci-main-passed` ruleset restricts updates to org/repo admins
  (CI uses `AF_RELEASE_TOKEN`).

### Retired pointers

Earlier drafts used a moving tag `main-ci-passed` and a custom ref
`refs/ci/main-passed`. Both are retired. If you still have a local tag
copy:

```
git tag -d main-ci-passed
```
