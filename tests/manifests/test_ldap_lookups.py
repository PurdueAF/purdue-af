"""Every LDAP uid/gid lookup in this repository must read the account's DN,
not search for it.

The directory has no uid index, and the Dask Gateway calls ldap_lookup on its
event loop, where a filtered search would stall the whole gateway.
"""

from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
BASE_DN = "ou=AllPeople,dc=geddes,dc=rcac,dc=purdue,dc=edu"
VALUES = REPO / "apps" / "dask-gateway" / "dask-gateway-k8s" / "values.yaml"


@pytest.fixture(scope="module")
def code() -> str:
    """The embedded python the gateway execs (gateway.extraConfig.config)."""
    return yaml.safe_load(VALUES.read_text())["gateway"]["extraConfig"]["config"]


def test_lookup_reads_the_dn_at_base_scope(code):
    assert 'search_base = "uid={0},{1}".format(username, baseDN)' in code
    assert "search_scope = BASE" in code


def test_lookup_falls_back_to_a_search_and_reports_a_miss(code):
    """An account not at its own DN is still resolved, and a genuine miss
    raises instead of IndexError-ing on entries[0]."""
    assert '"(uid={0}*)".format(username)' in code
    assert "search_scope = SUBTREE" in code
    assert 'raise ValueError("no LDAP entry for " + username)' in code
    assert "[u'entries'][0]" not in code


def test_lookup_targets_geddes_auth(code):
    assert 'url = "geddes-auth.rcac.purdue.edu"' in code
    assert f'baseDN = "{BASE_DN}"' in code


def test_embedded_gateway_config_compiles(code):
    compile(code, f"{VALUES}:gateway.extraConfig.config", "exec")
