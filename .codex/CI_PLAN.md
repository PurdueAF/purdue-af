# CI/CD Campaign Plan (Current State)

## Mission and Success Criteria
Deliver exactly one draft PR from `codex/ci` to `main` with minimal CI/CD hardening that:
- converts formatter-based CI to check-only behavior,
- adds advisory-first integrity/deploy/security coverage,
- keeps one source of truth in this file,
- preserves safe daily branch sync (`main` merged into `codex/ci`, no force-push).

Success means:
- `.github/workflows/lint-*.yml` workflows are check-only and run on every push + pull_request,
- new workflows exist for integrity, GitOps deployability, and nightly advisory security,
- optional repo-quality workflow is selected and included,
- README shows A-E category badges,
- no changes touch out-of-scope paths.

## In-Scope / Out-of-Scope Paths
In scope:
- `.github/**`
- `apps/**`
- `deploy/**`
- `docker/**` (except exclusions)
- `README.md`
- `.codex/CI_PLAN.md`

Out of scope:
- `docker/dask-gateway-server/**`
- `docs/**`
- `docs/source/demos/**`
- `docker/kaniko-build-jobs/**`
- `slurm/**`
- `.cursor/**`

## Target Check Architecture
### A) CI System Integrity (advisory)
- Workflow: `.github/workflows/ci-workflow-integrity.yml`
- Checks: `actionlint` + workflow YAML parse.
- Risk mapped: malformed workflows, invalid action definitions, skipped CI due syntax/runtime issues.

### B) Repo-Owned Code Quality / Tests (advisory additions)
- Workflows:
  - `.github/workflows/lint-python.yml`
  - `.github/workflows/lint-shell.yml`
  - `.github/workflows/lint-json.yml`
  - `.github/workflows/lint-yaml.yml`
  - `.github/workflows/ci-repo-quality.yml` (selected)
- Checks: black/isort check-only, py_compile, pytest (advisory), shellcheck/shfmt/bash -n, JSON/YAML parse checks.
- Risk mapped: runtime and script regressions.

### C) Container Reliability (advisory additions)
- Workflow: `.github/workflows/lint-docker.yml`
- Checks: hadolint (check-only), advisory docker build/smoke for maintained Dockerfiles via `.github/scripts/container-smoke.sh`.
- Risk mapped: container build/runtime breakage.

### D) GitOps/K8s Deployability (advisory)
- Workflow: `.github/workflows/ci-gitops-deployability.yml`
- Checks: `kustomize build --load-restrictor LoadRestrictionsNone` for all deploy overlays + `kubeconform` schema validation.
- Risk mapped: Flux reconciliation failures from invalid manifests.

### E) Nightly Advisory Security
- Workflow: `.github/workflows/nightly-security-advisory.yml`
- Checks: Trivy filesystem scan (HIGH/CRITICAL).
- Risk mapped: security posture drift.

## Advisory vs Future Blocking Milestones
- M0 (this campaign): all newly introduced validations advisory.
- M1: promote workflow integrity + repo-quality checks to blocking after stable baseline.
- M2: promote container + GitOps checks to blocking after stable baseline.
- M3: keep nightly security advisory unless explicitly promoted.

## Agent Lane Ownership (File Level)
- Coordinator: `.codex/CI_PLAN.md`, `README.md`, branch/PR/sync operations.
- Agent A: `.github/workflows/ci-workflow-integrity.yml` (+ selection recommendation in chat).
- Agent B: `.github/workflows/lint-python.yml`, `.github/workflows/lint-shell.yml`, `.github/workflows/ci-repo-quality.yml`, optional B helper scripts.
- Agent C: `.github/workflows/lint-json.yml`, `.github/workflows/lint-yaml.yml`.
- Agent D: `.github/workflows/lint-docker.yml`, `.github/scripts/container-smoke.sh`.
- Agent E: `.github/workflows/ci-gitops-deployability.yml`, `.github/workflows/nightly-security-advisory.yml`.

## Phased Rollout and Rollback
Rollout:
1. First commit creates this file.
2. Add/convert workflows in lane-owned files only.
3. Keep PR draft until baseline checks stabilize.
4. Daily sync by merging `main` into `codex/ci`.

Rollback:
- Revert only unstable workflow files in small commits.
- Keep advisory mode active during stabilization.

## Reproducible Runbook (from clean main)
1. `git fetch origin`
2. `git switch main && git pull --ff-only origin main`
3. `git switch -c codex/ci` (or `git switch codex/ci`)
4. Commit #1: `.codex/CI_PLAN.md`
5. Apply lane-scoped workflow changes
6. `git push -u origin codex/ci`
7. Open one draft PR `codex/ci -> main`
8. Daily sync: `git fetch origin && git switch codex/ci && git merge --no-ff origin/main`

## Constraint Challenge Protocol
If a hard constraint appears to conflict with delivery, create an `EXCEPTION REQUEST` with:
1) challenged constraint,
2) concrete risk if unchanged,
3) minimal exception,
4) rollback path.
Do not implement exception changes before explicit user approval.
