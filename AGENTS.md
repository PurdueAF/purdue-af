# Purdue Analysis Facility — repository guide

GitOps source of truth for the Purdue Analysis Facility: what the Geddes
clusters run is declared here, validated by one CI pipeline, and reconciled by
Flux.

Read before asking: [README.md](README.md) (what runs, and its live status),
[RELEASING.md](RELEASING.md) (how a change reaches a cluster, how to roll it
back), [deploy/README.md](deploy/README.md) (the Flux roots). This file holds
only what those do not say.

## Hard rules

- Namespace `cms`, always. Never create or inspect objects in another namespace.
- Never change what Flux manages. `kubectl edit|patch|scale|apply|rollout restart`
  and `flux suspend` of a HelmRelease are reverted on the next reconcile; fix
  the manifest. Never suspend a root Kustomization: that is not reverted, and
  every later merge silently stops deploying. The one sanctioned live write is
  deleting a component's pod to restart it.
- Never restart, delete or evict a user's session pod (`purdue-af-<id>`) or
  their Dask cluster. They hold long-running kernels — someone's work.
- Never move a tag or a branch by hand; [RELEASING.md](RELEASING.md) names the
  workflow that mints each one.
- Never commit a plaintext secret. A Secret under `apps/` is SOPS-encrypted per
  `.sops.yaml` and carries a `sops:` block.
- No real usernames in commits, PRs or test fixtures. Aggregate or redact first.
- Branch from `origin/main`, never from another PR's branch: when the base
  squash-merges, the PR retargets and its content never reaches `main`.
- The `[self-repair]` pull requests are the workflow's output for a person to
  judge. Never close, merge or comment on one; read them to improve the
  workflow.
- Carried verbatim, never edited: the Slurm RPMs and
  `slurm-configs-<cluster>/` trees copied from the clusters
  ([slurm/README.md](slurm/README.md)). The `pixi.lock` files under `pixi/`
  are CI-owned.

## Commands

```bash
uvx pre-commit run --all-files
uv run --project tests --frozen pytest -q -c tests/pyproject.toml tests
./.github/workflows/validate-manifests.sh
```

Lint, format and types (the hooks CI runs); the unit tests, from the repo root;
every Flux root rendered and validated. A change is done when the first two
pass — the third as well whenever `apps/` or `deploy/` changed. First-party
Python that is not listed in `files =` in `mypy.ini` is never type-checked. The
hub e2e needs a kind cluster: [tests/README.md](tests/README.md).

## Live cluster

- A merge to `main` deploys with no human step wherever a root tracks `main`
  or `main-validated` ([RELEASING.md](RELEASING.md)).
- Diagnose with reads: `kubectl -n cms get|describe|logs`; Loki for anything
  older than the pod (user streams carry a `username` label); the MCP server in
  [`.mcp.json`](.mcp.json) for session, storage, Dask and log state.
- Two Prometheus instances. The AF's own (`apps/monitoring/prometheus`) scrapes
  only its own `scrape_configs` and has no cAdvisor. Rancher's Prometheus holds
  `container_*`, node/pod resource series and every target behind the
  `scrape-metrics` ServiceMonitor, `purdue_af_mcp_*` included; it is reachable
  only from inside the cluster.

## Changes

- Commit subject: `<component>: <what changed>`, lowercase, no type prefixes —
  `af-node-monitor: set the stale-result window to 30 minutes`. A change with
  no single component takes a plain sentence. The body carries the reason.
- Version pins are Renovate's (`.github/renovate.json5` lists what it covers).
  Do not bump one inside an unrelated change; Renovate's PR runs the full
  pipeline for it.
- The component status badges in `README.md` are a static list, and each
  slug is derived from the component's directory. Adding, removing, renaming or
  moving a component means updating that list and any `LABEL_OVERRIDES` entry
  in `.github/workflows/component-status.py` naming the old path; the unit
  tests fail until both are right.
- The PR says what moves for users when it lands: a rolled pod, a new default
  environment, a changed quota or profile option.

## Writing

- One fact, one place. User-facing facts belong in `docs/` (how the site is
  written and built: `docs/docs/contributing.md`); how a change ships in
  `RELEASING.md`; conventions here. A README says what the component is, what
  its files are, how to run and tune it — never a "Why" section — and links
  out for everything else. Before writing a paragraph, check whether it
  already exists; if it does, link, don't copy.
- Sibling releases of one component (`apps/sonic/supersonic*`,
  `apps/interlink/*`) state a fact they share once — in the family README
  where there is one — not in every manifest. Workflow and script comments link to
  `RELEASING.md` rather than restate it.
- No prose counts nodes or GPUs; `docs/docs/hardware.md` is the one inventory.
- Present state, declaratively. Prose — READMEs, docs, comments — describes
  what is, never how it got there, what it replaced, or which alternative was
  rejected: git holds that. A sentence that reads like a changelog ("now",
  "no longer", "used to", "instead of the old …") is deleted or restated as
  the current fact.
- Inline comments are one line and rare: only where the code cannot say it —
  a non-obvious constraint, a workaround and what it works around. No comment
  restates the next line; no paragraph explains a function that its name and
  its test already explain. Rationale goes in the commit message.
- No test or hook asserts what prose says — Markdown, comments, docstrings,
  descriptions. A number quoted in prose is a copy nothing checks: quote it in
  its owner only, and grep for it when the source changes. Structure is
  checked: links resolve, nav entries exist, badge slugs name live components.
- `platform-context.md` is not this file: it tells an agent **inside a user's
  session** how the facility behaves. Repository conventions live here.

## Keeping this file

The same correction twice → one verifiable sentence here, or in a path-scoped
rule under `.claude/rules/` when it matters for one part of the tree only. What
an agent can read from the code does not belong here.
