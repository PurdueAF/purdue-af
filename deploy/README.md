## Continuous Deployment

The components of the Analysis Facility are divided into **core** and **experimental**:

- **Core**: stable and reliable services
- **Experimental**: subject to rapid prototyping

### Deployment strategy

- Core components are deployed into the production namespace `cms` from
  the newest platform CalVer tag (`YYYY.M.SEQ`).
- Experimental components are deployed from the moving git tag
  `main-ci-passed` (near tip of `main`, only advanced after the full
  pipeline is green) into the production namespace.

### Update process

- To update a core component, push to `main` — the CI pipeline validates
  the full state (the `ci-ok` gate). It reaches production when the next
  platform tag is minted — see [RELEASING.md](../RELEASING.md) for when
  and how versions are incremented (platform tags and image versions are
  minted by the release workflows, never by hand).
- To update an experimental component, push to `main` — after `ci-ok`
  succeeds, the publish stage moves `main-ci-passed` to that commit and
  Flux deploys it (experimental components are purposely brittle for
  faster prototyping, but still behind the same CI gate as image channels).