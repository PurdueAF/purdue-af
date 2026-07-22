## Continuous Deployment

The components of the Analysis Facility are divided into **core** and **experimental**:

- **Core**: stable and reliable services
- **Experimental**: subject to rapid prototyping

### Deployment strategy

- Core components are deployed into the production namespace `cms` from
  the newest platform CalVer tag (`YYYY.M.SEQ`).
- Experimental components are deployed from the CI-owned git ref
  `refs/ci/main-passed` (near tip of `main`, only advanced after the full
  pipeline is green) into the production namespace. This is a custom ref,
  not a tag — it is never included in `git push --tags` / `--all`.

### Update process

- To update a core component, push to `main` — the CI pipeline validates
  the full state (the `ci-ok` gate). It reaches production when the next
  platform tag is minted — see [RELEASING.md](../RELEASING.md) for when
  and how versions are incremented (platform tags and image versions are
  minted by the release workflows, never by hand).
- To update an experimental component, push to `main` — after `ci-ok`
  succeeds, the publish stage force-pushes `refs/ci/main-passed` to that
  commit and Flux deploys it (experimental components are purposely
  brittle for faster prototyping, but still behind the same CI gate as
  image channels).

### Retired: `main-ci-passed` tag

An earlier draft used a moving git tag named `main-ci-passed`. That tag
is retired (blocked from recreation on GitHub). If you still have a local
copy, drop it once:

```
git tag -d main-ci-passed
```
