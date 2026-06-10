"""End-to-end tests for userlist-sync/sync-userlist.sh with shimmed tools.

The script produces the Secrets that the spawner's auth gate reads, so the
refuse-to-write guards (empty / too-small / malformed lists) are the
behaviors under test. kubectl, ldapsearch and curl are PATH shims; jq and
the rest of the pipeline are real.
"""

import base64
import json
import subprocess
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "apps"
    / "jupyterhub"
    / "userlist-sync"
    / "sync-userlist.sh"
)

CRIC_FIXTURE = json.dumps(
    [
        {
            "profiles": [
                {"dn": "/DC=ch/DC=cern/OU=Users/CN=jdoe/CN=123/CN=John Doe"},
                {"dn": None},
            ]
        },
        {"profiles": [{"dn": "/DC=ch/DC=cern/OU=Users/CN=asmith/CN=456/CN=A Smith"}]},
        {"profiles": [{"dn": "/DC=ch/DC=cern/OU=Hosts/CN=not-a-user"}]},
    ]
)


@pytest.fixture
def shims(tmp_path):
    """PATH shims for the external tools; returns the call-log path."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    log.touch()

    def shim(name, body):
        path = bin_dir / name
        path.write_text(f'#!/bin/bash\necho "{name} $*" >>"$SHIM_LOG"\n{body}\n')
        path.chmod(0o755)

    shim(
        "kubectl",
        """
case "$*" in
  *jsonpath*) printf '%s' "$EXISTING_B64"; exit 0 ;;
  "get secret"*) exit "${GET_SECRET_RC:-1}" ;;
  "create secret generic"*)
    file="${@: -1}"; file="${file#userlist=}"; file="${file#--from-file=userlist=}"
    echo "created-with: $(sort "$file" | tr '\\n' ',')" >>"$SHIM_LOG"; exit 0 ;;
  *) exit 0 ;;
esac
""",
    )
    shim("ldapsearch", 'seq -f "uid: user%g" 1 "${LDAP_COUNT:-250}"')
    shim("curl", 'printf "%s" "$CRIC_JSON"')

    return {"bin": bin_dir, "log": log}


def run_sync(shims, source, **env):
    import os

    full_env = {
        **os.environ,
        "PATH": f"{shims['bin']}:{os.environ['PATH']}",
        "SHIM_LOG": str(shims["log"]),
        "CRIC_JSON": CRIC_FIXTURE,
        **{k: str(v) for k, v in env.items()},
    }
    return subprocess.run(
        ["bash", str(SCRIPT), source],
        env=full_env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def calls(shims):
    return shims["log"].read_text()


# ── purdue (LDAP) ─────────────────────────────────────────────────────────────


def test_creates_secret_when_absent(shims):
    result = run_sync(shims, "purdue", GET_SECRET_RC=1, LDAP_COUNT=250)

    assert result.returncode == 0, result.stderr
    assert "Found 250 users" in result.stdout
    assert "kubectl create secret generic af-auth-purdue" in calls(shims)


def test_updates_existing_secret_and_reports_diff(shims):
    existing = base64.b64encode(b"user1\nuser2\ngone-user\n").decode()
    result = run_sync(
        shims, "purdue", GET_SECRET_RC=0, EXISTING_B64=existing, LDAP_COUNT=250
    )

    assert result.returncode == 0, result.stderr
    assert "Removed users:" in result.stdout and "gone-user" in result.stdout
    assert "Added users:" in result.stdout
    assert "kubectl patch secret af-auth-purdue" in calls(shims)


def test_refuses_small_list(shims):
    result = run_sync(shims, "purdue", LDAP_COUNT=12)

    assert result.returncode == 1
    assert "refusing to update" in result.stdout
    assert "patch" not in calls(shims) and "create" not in calls(shims)


def test_refuses_empty_list(shims):
    result = run_sync(shims, "purdue", LDAP_COUNT=0)

    assert result.returncode == 1
    assert "patch" not in calls(shims) and "create" not in calls(shims)


def test_min_users_is_tunable(shims):
    result = run_sync(shims, "purdue", GET_SECRET_RC=1, LDAP_COUNT=12, MIN_USERS=10)
    assert result.returncode == 0, result.stderr


# ── cern (CRIC + jq/sed extraction) ───────────────────────────────────────────


def test_cern_extracts_usernames_from_dns(shims):
    result = run_sync(shims, "cern", GET_SECRET_RC=1, MIN_USERS=2)

    assert result.returncode == 0, result.stderr
    assert "Found 2 users" in result.stdout  # null DN and OU=Hosts excluded
    assert "created-with: asmith,jdoe," in calls(shims)


def test_requires_source_argument(shims):
    result = run_sync(shims, "")
    assert result.returncode != 0
