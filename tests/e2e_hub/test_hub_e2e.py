"""End-to-end: real hub, real chart, real auth scripts — mock CILogon.

The production custom-spawner.py runs unmodified inside the hub; these tests
drive it through the actual OAuth code flow and the KubeSpawner spawn path.
"""

import time

from conftest import CERN_IDP, HUB, MOCK, PURDUE_IDP


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


# ── session lifecycle & isolation (tests below build on alice's running server) ──


def kubectl_json(*args):
    import json
    import subprocess

    return json.loads(
        subprocess.run(
            ["kubectl", *args, "-o", "json"], capture_output=True, text=True, check=True
        ).stdout
    )


def singleuser_pods():
    return kubectl_json("get", "pods", "-l", "component=singleuser-server")["items"]


def wait_ready(admin, user, timeout=300):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        servers = admin.get(f"/hub/api/users/{user}").json().get("servers", {})
        if servers.get("", {}).get("ready"):
            return
        time.sleep(5)
    raise AssertionError(f"{user}'s server never became ready")


def test_ldap_uid_gid_land_in_pod(admin):
    """set-user-info.py auth_state_hook: the LDAP-resolved uid/gid of a
    Purdue user must reach the pod env (values from mock-ldap-users.ldif).
    Runs against alice's server spawned above."""
    pod = next(
        p
        for p in singleuser_pods()
        if p["metadata"]["labels"].get("username_unescaped") == "alice"
    )
    env = {e["name"]: e.get("value") for e in pod["spec"]["containers"][0]["env"]}
    assert env.get("NB_USER") == "alice"
    assert env.get("NB_UID") == "20001"
    assert env.get("NB_GID") == "21001"


def test_second_user_spawns_concurrently(login, admin):
    """Two users' pods coexist, each carrying its own ownership label."""
    login("carol@cern.ch", idp=CERN_IDP)
    spawn = admin.post("/hub/api/users/carol-cern/server", json={})
    assert spawn.status_code in (201, 202), spawn.text
    wait_ready(admin, "carol-cern")

    labels = {
        p["metadata"]["labels"].get("username_unescaped") for p in singleuser_pods()
    }
    assert {"alice", "carol-cern"} <= labels


def test_profile_selection_lands_in_pod(admin):
    """Explicit profile choice (the start_af_session contract) must reach the pod."""
    admin.delete("/hub/api/users/alice/server")
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if not admin.get("/hub/api/users/alice").json().get("servers"):
            break
        time.sleep(3)

    spawn = admin.post(
        "/hub/api/users/alice/server", json={"profile": "e2e-alt-profile"}
    )
    assert spawn.status_code in (201, 202), spawn.text
    wait_ready(admin, "alice")

    pod = next(
        p
        for p in singleuser_pods()
        if p["metadata"]["labels"].get("username_unescaped") == "alice"
    )
    env = {e["name"]: e.get("value") for e in pod["spec"]["containers"][0]["env"]}
    assert env.get("E2E_PROFILE_MARKER") == "alt"


def test_stop_server_cleans_up(admin):
    """Stopping must remove the pod and clear hub state (carol's untouched)."""
    stop = admin.delete("/hub/api/users/alice/server")
    assert stop.status_code in (202, 204), stop.text

    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        owners = {
            p["metadata"]["labels"].get("username_unescaped")
            for p in singleuser_pods()
            if not p["metadata"].get("deletionTimestamp")
        }
        if "alice" not in owners:
            break
        time.sleep(3)
    else:
        raise AssertionError("alice's pod was never deleted")

    assert "carol-cern" in owners  # isolation: other user's session survives
    assert admin.get("/hub/api/users/alice").json().get("servers", {}) == {}


def test_userlist_update_allows_new_user_without_restart(login):
    """The userlist-sync pipeline's core assumption: updating the secret is
    enough — the auth gate reads it per-login, no hub restart needed.
    (Kubelet syncs secret volumes with up to ~1 min delay.)"""
    import base64
    import subprocess

    users = base64.b64encode(b"alice\nbob\ndkondra\ndave\n").decode()
    subprocess.run(
        [
            "kubectl",
            "patch",
            "secret",
            "af-auth-purdue",
            "--type=merge",
            "-p",
            '{"data":{"userlist":"%s"}}' % users,
        ],
        check=True,
        capture_output=True,
    )

    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        _, response = login("dave@purdue.edu")
        if response.status_code == 200:
            return
        time.sleep(10)
    raise AssertionError("dave never became allowed after secret update")


def test_admin_user_flag_is_wired(login, admin):
    """values.yaml admin_users must translate into the hub admin flag."""
    login("dkondra@purdue.edu")
    user = admin.get("/hub/api/users/dkondra").json()
    assert user.get("admin") is True


def test_tampered_oauth_state_is_rejected(login):
    """CSRF gate: a forged callback (bad state) must not create a session."""
    import httpx

    client = httpx.Client(base_url=HUB, follow_redirects=True, timeout=30)
    response = client.get("/hub/oauth_callback?code=forged&state=garbage")
    client.close()
    assert response.status_code == 400


def test_logout_clears_session(login):
    """auto_login means logout is followed by silent re-auth on the next
    request — so prove the OLD session died by making the re-auth identity
    a denied user. A surviving cookie would return 200 without consulting
    the mock; a real logout forces the OAuth round trip into the gate."""
    import httpx

    client, _ = login("alice@purdue.edu")
    assert client.get("/hub/home").status_code == 200

    client.get("/hub/logout", follow_redirects=False)
    httpx.post(
        f"{MOCK}/_identity",
        json={"eppn": "mallory@purdue.edu", "idp": PURDUE_IDP},
        timeout=10,
    )
    response = client.get("/hub/home")  # follows into OAuth as mallory
    assert response.status_code in (403, 500)  # denied: old session is gone
