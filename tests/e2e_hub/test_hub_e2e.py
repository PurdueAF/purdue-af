"""End-to-end: real hub, real chart, real auth scripts — mock CILogon.

The production custom-spawner.py runs unmodified inside the hub; these tests
drive it through the actual OAuth code flow and the KubeSpawner spawn path.
"""

import time

from conftest import CERN_IDP, HUB


def test_hub_api_is_up(admin):
    response = admin.get("/hub/api/")
    assert response.status_code == 200
    assert "version" in response.json()


def test_purdue_user_in_list_logs_in(login):
    client, response = login("alice@purdue.edu")

    # Full round trip: /hub/login -> mock authorize -> callback -> hub session.
    assert response.status_code == 200
    assert "/hub/" in str(response.url) or "/user/" in str(response.url)

    home = client.get("/hub/home")
    assert home.status_code == 200
    assert "alice" in home.text


def test_purdue_user_not_in_list_is_rejected(login):
    _, response = login("mallory@purdue.edu")
    # The deployed (pre-incident) spawner raises 500 for unlisted users; the
    # reverted-and-pending rewrite returns 403. Both mean "kept out".
    assert response.status_code in (403, 500)


def test_cern_user_gets_suffixed_username(login, admin):
    client, response = login("carol@cern.ch", idp=CERN_IDP)
    assert response.status_code == 200

    user = admin.get("/hub/api/users/carol-cern")
    assert user.status_code == 200, "expected hub user 'carol-cern' to exist"


def test_unknown_domain_is_rejected(login):
    _, response = login("eve@evil.example")
    assert response.status_code in (403, 500)


def test_spawn_reaches_running_jupyterlab(login, admin):
    client, _ = login("alice@purdue.edu")

    spawn = admin.post("/hub/api/users/alice/server", json={})
    assert spawn.status_code in (201, 202), spawn.text

    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        servers = admin.get("/hub/api/users/alice").json().get("servers", {})
        if servers.get("", {}).get("ready"):
            break
        time.sleep(5)
    else:
        raise AssertionError(f"server never became ready: {servers}")

    # The user's own session must reach the running Lab server.
    lab = client.get(f"{HUB}/user/alice/api")
    assert lab.status_code == 200
    assert "version" in lab.json()


def test_spawned_pod_carries_ownership_label(admin):
    """The username_unescaped label is the agentic-interface ownership gate."""
    import json
    import subprocess

    pods = json.loads(
        subprocess.run(
            [
                "kubectl",
                "get",
                "pods",
                "-l",
                "component=singleuser-server",
                "-o",
                "json",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    assert pods["items"], "no singleuser pod found"
    labels = pods["items"][0]["metadata"]["labels"]
    assert labels.get("username_unescaped") == "alice"
