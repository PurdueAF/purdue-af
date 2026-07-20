## Continuous Deployment

The components of the Analysis Facility are divided into **core** and **experimental**:

- **Core**: stable and reliable services
- **Experimental**: subject to rapid prototyping

### Deployment strategy

- Core components are deployed from the head of the `main` branch into the staging namespace `cms-dev`.
- Core components are also deployed into the production namespace `cms` from the latest explicitly tagged commit.
- Experimental components are deployed from the head of the `main` branch into the production namespace.

### Update process

- To update a core component, push to `main` and verify it in the staging
  namespace (`cms-dev`, which tracks `main` head). It reaches production
  when the next platform CalVer tag is minted — see
  [RELEASING.md](../RELEASING.md) for when and how versions are
  incremented (platform tags and image versions are minted by the release
  workflows, never by hand).
- To update an experimental component, push to `main` — it deploys to the
  production namespace directly (experimental components are purposely
  brittle for faster prototyping).