## Continuous Deployment

The components of the Analysis Facility can be roughly divided into "core" and "experimental";
the "core" components are intended to be stable and reliable, while the "experimental" components
are subject to rapid prototyping.

The deployment strategy is as follows:

- the "core" components are deployed from the head of the main branch into staging namespace `cms-dev`
- the "core" components are also deployed into production namespace `cms` from the latest explicitly tagged commit
- the "experimental" components are deployed from the head of the main branch into the production namespace

Updates to the deployed components happen as follows:
- To update a core component, we push directly to main branch and make sure that everything looks good in the
  staging namespace. Then, we tag the successful commit using `YYYY.MM.v` versioning scheme, which triggers
  deployment into production namespace.
- To apply a hot fix, we branch out of the tagged commit, apply the fix and tag it with a newer version. The fix should be merged into main branch later.
- To update an experimental component, we just push to main branch.