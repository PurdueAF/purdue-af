## Continuous Deployment

The components of the Analysis Facility are divided into **core** and **experimental**:

- **Core**: stable and reliable services
- **Experimental**: subject to rapid prototyping

### Deployment strategy

- Core components are deployed from the head of the `main` branch into the staging namespace `cms-dev`.
- Core components are also deployed into the production namespace `cms` from the latest explicitly tagged commit.
- Experimental components are deployed from the head of the `main` branch into the production namespace.

### Update process

- To update a core component, push directly to the `main` branch and verify everything looks good in the
  staging namespace. Then tag the successful commit using the `YYYY.MM.v` versioning scheme, which triggers
  deployment into the production namespace.
- To apply a hot fix, branch from the tagged commit, apply the fix, and tag it with a newer version. The fix should be merged back into `main` later.
- To update an experimental component, push to the `main` branch.