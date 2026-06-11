"""Fixtures for the hub e2e suite — requires a running kind environment.

Skipped entirely unless E2E_HUB=1 (set by the Hub E2E workflow / manual runs),
so the regular unit-test job never touches these.
"""

import os

import httpx
import pytest

if not os.environ.get("E2E_HUB"):
    pytest.skip("E2E_HUB not set — kind environment required", allow_module_level=True)

HUB = os.environ.get("E2E_HUB_URL", "http://localhost:8080")
MOCK = os.environ.get("E2E_MOCK_URL", "http://localhost:9090")
ADMIN_TOKEN = "e2e-admin-token-not-a-secret"  # values-kind.yaml services.e2e

PURDUE_IDP = "https://idp.purdue.edu/idp/shibboleth"
CERN_IDP = "https://cern.ch/login"


@pytest.fixture
def admin():
    """Client authenticated as the e2e admin service."""
    with httpx.Client(
        base_url=HUB, headers={"Authorization": f"token {ADMIN_TOKEN}"}, timeout=30
    ) as client:
        yield client


@pytest.fixture
def login():
    """Factory: run the full OAuth flow for an identity; returns the
    browser-equivalent client (cookies and all) plus the final response."""
    clients = []

    def _login(eppn, idp=PURDUE_IDP):
        httpx.post(f"{MOCK}/_identity", json={"eppn": eppn, "idp": idp}, timeout=10)
        client = httpx.Client(base_url=HUB, follow_redirects=True, timeout=30)
        clients.append(client)
        response = client.get("/hub/login")
        return client, response

    yield _login
    for client in clients:
        client.close()
