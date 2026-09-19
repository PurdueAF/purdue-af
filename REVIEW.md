# Review contract

What a review of this repository checks, whether a person or an agent does it.
Conventions are in [AGENTS.md](AGENTS.md); this is what to look for and how to
report it.

## Passes, in order

1. **Correctness** — logic, edge cases, error paths; what happens when a
   dependency is down, slow or absent. Does the change do what the PR says, and
   nothing else?
2. **Reconcile** — will it reach the cluster, and only what it claims? The
   Flux root (core tag vs `main-validated`), every new file listed in
   `deploy/`, `dependsOn`, `reconcileStrategy`, `postBuild` variables,
   generated ConfigMap names, namespace `cms`. A patch that matches nothing is
   the classic silent failure here.
3. **Blast radius** — what moves for users when it lands: a restarted hub, a
   rolled agentic-interface pod, a new default environment, a changed quota or
   profile option. The PR must say so; anything that interrupts a running
   session needs a reason that outweighs it.
4. **Secrets and privacy** — plaintext credentials or tokens; a Secret without
   a `sops:` block; real usernames in code, fixtures or the PR text.
5. **Tests** — is the new behaviour asserted, and does the assertion re-derive
   its expectation from the source rather than restate it? A bug fix without a
   test that failed before it is incomplete.
6. **Drift** — the duplicates kept on purpose: the README badge list and
   `LABEL_OVERRIDES`, `platform-context.md` numbers, the pixi version pins,
   user docs quoting values.
7. **Prose** — READMEs, docs and comments describe the present state,
   declaratively. A changelog sentence, an incident story, a rejected
   alternative, or a paragraph that already exists elsewhere is a finding; the
   commit carries those.

## Important

A finding is important if it breaks reconcile, interrupts or changes a user's
session, leaks a secret or a username, or ships a behaviour change nothing
asserts. Everything else is minor, however tidy.

## Report

Findings first, ordered by severity, each with `file:line`, the defect in one
sentence, and the concrete input or state that triggers it. Then at most five
nits; the rest as a count. No praise, no restating the diff, no style findings
pre-commit already enforces.

## Out of scope

- What AGENTS.md's hard rules say is carried verbatim or CI-owned.
- Formatting, import order, shell and Dockerfile style: pre-commit decides.
- Generated badge data on the `status` branch.
