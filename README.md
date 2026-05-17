# Purdue Analysis Facility

[Analysis Facility entry point and documentation](https://analysis-facility.physics.purdue.edu)

The Purdue Analysis Facility is designed to provide an interactive environment for fast and scalable CMS physics analyses using dedicated computing resources at Purdue Tier-2 computing cluster.

The following login options are supported:

- Purdue University account (BoilerKey)
- CERN account (CMS users only)
- FNAL account

The same person using different accounts to sign in will be treated as different users.

Each user is provided with a 25GB home directory at first login. These directories will persist between sessions, but will be deleted after 6 months of inactivity.

---

[![Documentation Status](https://readthedocs.org/projects/purdue-af/badge/?version=latest)](https://purdue-af.readthedocs.io/en/latest/?badge=latest)

### Runtime Status

[![Workflow Integrity](https://github.com/PurdueAF/purdue-af/actions/workflows/ci-workflow-integrity.yml/badge.svg?branch=main)](https://github.com/PurdueAF/purdue-af/actions/workflows/ci-workflow-integrity.yml)
[![Repo Quality](https://github.com/PurdueAF/purdue-af/actions/workflows/ci-repo-quality.yml/badge.svg?branch=main)](https://github.com/PurdueAF/purdue-af/actions/workflows/ci-repo-quality.yml)
[![CI Format Autofix](https://github.com/PurdueAF/purdue-af/actions/workflows/ci-format-autofix.yml/badge.svg?branch=main)](https://github.com/PurdueAF/purdue-af/actions/workflows/ci-format-autofix.yml)
[![CI Integration Scenarios](https://github.com/PurdueAF/purdue-af/actions/workflows/ci-integration-scenarios.yml/badge.svg?branch=main)](https://github.com/PurdueAF/purdue-af/actions/workflows/ci-integration-scenarios.yml)
[![Container Reliability](https://github.com/PurdueAF/purdue-af/actions/workflows/lint-docker.yml/badge.svg?branch=main)](https://github.com/PurdueAF/purdue-af/actions/workflows/lint-docker.yml)
[![GitOps Deployability](https://github.com/PurdueAF/purdue-af/actions/workflows/ci-gitops-deployability.yml/badge.svg?branch=main)](https://github.com/PurdueAF/purdue-af/actions/workflows/ci-gitops-deployability.yml)
[![CI Security Advisory](https://github.com/PurdueAF/purdue-af/actions/workflows/ci-security-advisory.yml/badge.svg?branch=main)](https://github.com/PurdueAF/purdue-af/actions/workflows/ci-security-advisory.yml)
[![Nightly Security](https://github.com/PurdueAF/purdue-af/actions/workflows/nightly-security-advisory.yml/badge.svg?branch=main)](https://github.com/PurdueAF/purdue-af/actions/workflows/nightly-security-advisory.yml)

### Policy Badges

[![Coverage Gate](https://img.shields.io/badge/Coverage%20Gate-%3E%3D70%25%20%28advisory%29-4c1)](https://github.com/PurdueAF/purdue-af/actions/workflows/ci-repo-quality.yml)
[![Security Scans](https://img.shields.io/badge/Security%20Scans-PR%20%2B%20Nightly-0366d6)](https://github.com/PurdueAF/purdue-af/actions/workflows/ci-security-advisory.yml)
[![Validation Mode](https://img.shields.io/badge/Validation%20Mode-Advisory--first-f59e0b)](https://github.com/PurdueAF/purdue-af/actions)
[![Autofix](https://img.shields.io/badge/Autofix-Python%2FShell%2FJSON%2FYAML-7c3aed)](https://github.com/PurdueAF/purdue-af/actions/workflows/ci-format-autofix.yml)

### CI Profile

| Signal | Workflow | Trigger | Mode (advisory/blocking) | Notes |
|---|---|---|---|---|
| Workflow integrity | [CI Workflow Integrity](https://github.com/PurdueAF/purdue-af/actions/workflows/ci-workflow-integrity.yml) | Pull request (`.github/workflows/**`) | advisory | Actionlint + workflow YAML parse |
| Repo quality | [CI Repo Quality](https://github.com/PurdueAF/purdue-af/actions/workflows/ci-repo-quality.yml) | Pull request (unit/runtime paths) | advisory | Unit tests + 70% coverage policy signal |
| Format autofix | [CI Format Autofix](https://github.com/PurdueAF/purdue-af/actions/workflows/ci-format-autofix.yml) | Pull request open/sync/reopen (format-targeted paths) | advisory | Auto-formats and pushes fix commits to PR branch |
| Integration scenarios | [CI Integration Scenarios](https://github.com/PurdueAF/purdue-af/actions/workflows/ci-integration-scenarios.yml) | Pull request (integration paths) | advisory | Scripted integration scenario run |
| Container reliability | [Container Reliability](https://github.com/PurdueAF/purdue-af/actions/workflows/lint-docker.yml) | Pull request (container/slurm paths) | advisory | Hadolint + image build/smoke checks |
| GitOps deployability | [CI GitOps Deployability](https://github.com/PurdueAF/purdue-af/actions/workflows/ci-gitops-deployability.yml) | Pull request (`deploy/**`) | advisory | Kustomize render + kubeconform validation |
| Security advisory (PR) | [CI Security Advisory](https://github.com/PurdueAF/purdue-af/actions/workflows/ci-security-advisory.yml) | Pull request (security-relevant paths) + manual dispatch | advisory | Trivy vuln/config scans with summary + artifacts |
| Security advisory (nightly) | [Nightly Security Advisory](https://github.com/PurdueAF/purdue-af/actions/workflows/nightly-security-advisory.yml) | Nightly schedule + manual dispatch | advisory | Trivy filesystem scan with nightly summary |
