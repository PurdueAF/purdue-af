# Tests

One uv project, one lock, every suite. Run from the repository root so coverage
records repo-rooted paths (scope in [`.coveragerc`](../.coveragerc)):

```bash
uv run --project tests --frozen pytest -q -c tests/pyproject.toml tests            # everything CI runs
uv run --project tests --frozen pytest -q -c tests/pyproject.toml tests/manifests  # one suite
uv run --project tests mypy                                                         # types — scope and flags in mypy.ini
```

CI runs the first line, plus coverage, in stage 0
([`ci-checks.yml`](../.github/workflows/ci-checks.yml)). That run is
cluster-free and offline: HTTP is mocked with `respx`, Kubernetes objects are
parsed as YAML, and nothing reaches Geddes. Each directory is one suite, named
after what it tests (`docs/` asserts the site's navigation against its pages). Two have their own README:
[`e2e_hub/`](e2e_hub/README.md) (the real hub in kind, skipped unless
`E2E_HUB=1`) and [`integration_challenge/`](integration_challenge/README.md).

## Conventions

- Shared plumbing is [`common.py`](common.py): `REPO` (repository root) and
  `load_script()` for importing files whose names are not importable.
- Per-suite fixtures go in that directory's `conftest.py`; per-suite helpers in
  a uniquely named `*_helpers.py` (`hub_helpers.py`, `script_helpers.py`),
  because several modules called `conftest` cannot be imported from test code.
- A test asserts the behaviour, not the implementation: manifest tests
  re-derive the expected value from its source file rather than restating it,
  so the test fails when the two drift, not when the wording changes.

## Adding a suite

Create `tests/<suite>/`, add any runtime dependency of the code under test to
[`pyproject.toml`](pyproject.toml) (this project installs the dependencies of
the sources it tests, not just the test tooling), and `uv lock --project tests`.
Add the component to `component_management` in [`codecov.yml`](../codecov.yml)
so its coverage is reported on its own.
