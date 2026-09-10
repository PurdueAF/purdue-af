"""Every LDAP uid/gid lookup must read the account's DN, not search for it.

geddes-auth carries no index on uid, so a filtered search under ou=AllPeople
scans ~69k entries and takes 15-25s. Both gateways call ldap_lookup from a
synchronous options_handler on their event loop, so one such search stalls the
whole gateway — the same way it stalled the Hub (see
tests/jupyterhub_config/test_set_user_info.py).
"""

from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
BASE_DN = "ou=AllPeople,dc=geddes,dc=rcac,dc=purdue,dc=edu"

GATEWAY_VALUES = {
    "k8s": REPO / "apps" / "dask-gateway" / "dask-gateway-k8s" / "values.yaml",
    "interlink": REPO
    / "apps"
    / "dask-gateway"
    / "dask-gateway-k8s-interlink"
    / "values.yaml",
}
SLURM_BACKEND = (
    REPO
    / "docker"
    / "dask-gateway-server"
    / "dask_gateway_server"
    / "backends"
    / "jobqueue"
    / "slurm.py"
)


def gateway_config(path: Path) -> str:
    """The embedded python the gateway execs (gateway.extraConfig.config)."""
    return yaml.safe_load(path.read_text())["gateway"]["extraConfig"]["config"]


ALL_SOURCES = [
    pytest.param(gateway_config, path, id=name) for name, path in GATEWAY_VALUES.items()
] + [pytest.param(Path.read_text, SLURM_BACKEND, id="slurm-backend")]


@pytest.mark.parametrize("read,path", ALL_SOURCES)
def test_lookup_reads_the_dn_at_base_scope(read, path):
    code = read(path)
    assert 'search_base = "uid={0},{1}".format(username, baseDN)' in code
    assert "search_scope = BASE" in code


@pytest.mark.parametrize("read,path", ALL_SOURCES)
def test_lookup_falls_back_to_a_search_and_reports_a_miss(read, path):
    """An account not at its own DN is still resolved, and a genuine miss
    raises instead of IndexError-ing on entries[0]."""
    code = read(path)
    assert '"(uid={0}*)".format(username)' in code
    assert "search_scope = SUBTREE" in code
    assert 'raise ValueError("no LDAP entry for " + username)' in code
    assert "[u'entries'][0]" not in code


@pytest.mark.parametrize("read,path", ALL_SOURCES)
def test_lookup_targets_geddes_auth(read, path):
    code = read(path)
    assert 'url = "geddes-auth.rcac.purdue.edu"' in code
    assert f'baseDN = "{BASE_DN}"' in code


@pytest.mark.parametrize("name,path", sorted(GATEWAY_VALUES.items()))
def test_embedded_gateway_config_compiles(name, path):
    code = gateway_config(path)
    compile(code, f"{name}:gateway.extraConfig.config", "exec")
