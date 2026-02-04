# CI/CD Campaign Plan (Current State)

## Mission
Deliver one draft PR from `codex/ci` to `main` with a stable advisory-first CI baseline, then optimize test depth, integration realism, and security signal without broad refactors.

## Current Status
- PR branch: `codex/ci`
- Delivery model: single PR `codex/ci -> main`
- Existing CI baseline is green on PR checks.
- Formatter/linter workflows are check-only (no CI writeback commits).

## Success Criteria
- CI remains stable on `pull_request` runs for all configured workflows.
- Optimization phase adds meaningful unit and integration coverage for repo-owned code.
- Security checks include nightly advisory plus PR-time advisory signal.
- `README.md` keeps A-E category badges aligned with active workflows.
- `.codex/CI_PLAN.md` remains the single source of truth.

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

Approved exception:
- `slurm/**` is used as a dependency-only trigger in container reliability path filters because maintained Dockerfiles copy `slurm/` artifacts.

## Active Workflow Surface
- `.github/workflows/ci-workflow-integrity.yml`
- `.github/workflows/lint-python.yml`
- `.github/workflows/lint-shell.yml`
- `.github/workflows/lint-json.yml`
- `.github/workflows/lint-yaml.yml`
- `.github/workflows/ci-repo-quality.yml`
- `.github/workflows/lint-docker.yml`
- `.github/workflows/ci-gitops-deployability.yml`
- `.github/workflows/nightly-security-advisory.yml`

## Check Architecture
### A) CI System Integrity (advisory)
- Workflow: `ci-workflow-integrity.yml`
- Checks: actionlint + workflow YAML parse.
- Risk: broken workflow definitions and silent CI drift.

### B) Repo Quality and Tests (advisory)
- Workflows: `lint-python.yml`, `lint-shell.yml`, `lint-json.yml`, `lint-yaml.yml`, `ci-repo-quality.yml`
- Checks: black/isort check-only, py_compile, pytest advisory, shellcheck/shfmt/bash -n, JSON/YAML parse.
- Risk: script/runtime regressions.

### C) Container Reliability (advisory)
- Workflow: `lint-docker.yml`
- Checks: hadolint, targeted docker build jobs, smoke checks via `.github/scripts/container-smoke.sh`.
- Risk: image build/runtime regressions.

### D) GitOps Deployability (advisory)
- Workflow: `ci-gitops-deployability.yml`
- Checks: kustomize render + kubeconform schema validation.
- Risk: Flux reconciliation failures from invalid manifests.

### E) Security Posture (advisory)
- Workflow: `nightly-security-advisory.yml`
- Checks: nightly Trivy filesystem scan.
- Risk: security drift in dependencies/configuration.

## Optimization Workstreams (Current)
### Worker 1: Coverage Optimizer
File lane:
- `tests/unit/**`
- `tests/conftest.py`
- `.github/workflows/lint-python.yml`
- `.github/workflows/ci-repo-quality.yml`
Goal:
- Increase meaningful Python test coverage and publish coverage in CI (advisory threshold first).

### Worker 2: Integration Scenarios
File lane:
- `tests/integration/**`
- `tests/fixtures/**`
- `.github/workflows/ci-integration-scenarios.yml` (new)
- `.github/scripts/integration/**`
Goal:
- Add realistic automated integration scenarios with deterministic mocks and PR advisory execution.

### Worker 3: Security and Runtime Optimizer
File lane:
- `.github/workflows/nightly-security-advisory.yml`
- `.github/workflows/ci-security-advisory.yml` (new)
- `.github/workflows/lint-docker.yml`
- `.github/workflows/ci-gitops-deployability.yml`
Goal:
- Add PR-time advisory security checks and reduce CI runtime/noise safely.

## Branch and Sync Rules
- No side branches.
- No force-push on shared campaign work.
- Daily sync: merge `main` into `codex/ci` (no rebase).
- Keep PR draft until optimization baseline is stable.

## Constraint Challenge Protocol
If any hard constraint must be challenged, submit an `EXCEPTION REQUEST` with:
1) challenged constraint,
2) concrete risk if unchanged,
3) minimal exception requested,
4) rollback path.

No exception is implemented without explicit user approval.
