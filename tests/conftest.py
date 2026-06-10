"""Root conftest: anchors tests/ on sys.path so suites can import common.py.

Suite-specific fixtures live in each directory's conftest; shared plumbing
in common.py; per-suite helpers in <suite>_helpers.py (uniquely named —
multiple modules called `conftest` cannot be imported from test code).
"""
