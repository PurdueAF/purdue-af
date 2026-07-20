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


def test_production_spawn_carries_ownership_label(login, admin):
    """Base of the plumbing chain: spawn alice on the DEFAULT profile (the
    production image, no mock stand-in) and assert the username_unescaped
    label — the agentic-interface ownership gate. Real image, so gated on
    E2E_PRODUCTION (preloaded in the e2e-production job; skipped elsewhere).
    That a real AF session reaches a running Lab is asserted by
    test_production_profile_spawns_pinned_image below."""
    require_production()
    login("alice@purdue.edu")
    admin.delete("/hub/api/users/alice/server")  # tolerate leftovers (404 ok)
    wait_cleared(admin, "alice")

    spawn = admin.post("/hub/api/users/alice/server", json={})
    assert spawn.status_code in (201, 202), spawn.text
    wait_ready(admin, "alice", timeout=600)

    pod = next(
        p
        for p in singleuser_pods()
        if p["metadata"]["labels"].get("username_unescaped") == "alice"
    )
    assert pod["metadata"]["labels"].get("username_unescaped") == "alice"


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


def wait_cleared(admin, user, timeout=120):
    """Wait until `user` has no server (after a delete, before a respawn)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not admin.get(f"/hub/api/users/{user}").json().get("servers"):
            return
        time.sleep(3)


def require_production():
    """Skip unless the real production AF image is preloaded on the node
    (E2E_PRODUCTION, set by the e2e-production CI job). The spawn-based
    plumbing tests run the REAL image — there is no mock stand-in — so they
    only run where it is available."""
    import os

    import pytest

    if not os.environ.get("E2E_PRODUCTION"):
        pytest.skip("production AF image not preloaded (E2E_PRODUCTION unset)")


def test_ldap_uid_gid_land_in_pod(admin):
    """set-user-info.py auth_state_hook: the LDAP-resolved uid/gid of a
    Purdue user must reach the pod env (values from mock-ldap-users.ldif).
    Runs against alice's server spawned above."""
    require_production()
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
    require_production()
    login("carol@cern.ch", idp=CERN_IDP)
    spawn = admin.post("/hub/api/users/carol-cern/server", json={})
    assert spawn.status_code in (201, 202), spawn.text
    wait_ready(admin, "carol-cern", timeout=600)

    labels = {
        p["metadata"]["labels"].get("username_unescaped") for p in singleuser_pods()
    }
    assert {"alice", "carol-cern"} <= labels


def test_profile_selection_lands_in_pod(admin):
    """Explicit profile choice (the start_af_session contract) must reach the pod."""
    require_production()
    admin.delete("/hub/api/users/alice/server")
    wait_cleared(admin, "alice")

    spawn = admin.post(
        "/hub/api/users/alice/server", json={"profile": "e2e-alt-profile"}
    )
    assert spawn.status_code in (201, 202), spawn.text
    wait_ready(admin, "alice", timeout=600)

    pod = next(
        p
        for p in singleuser_pods()
        if p["metadata"]["labels"].get("username_unescaped") == "alice"
    )
    env = {e["name"]: e.get("value") for e in pod["spec"]["containers"][0]["env"]}
    assert env.get("E2E_PROFILE_MARKER") == "alt"


def test_stop_server_cleans_up(admin):
    """Stopping must remove the pod and clear hub state (carol's untouched)."""
    require_production()
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


def test_prerelease_profile_spawns_ci_built_image(login, admin):
    """The CD gate for the AF image: the pre-release JH option must spawn the
    exact image this pipeline built (sha-<commit> if rebuilt in this commit,
    else the promoted :pre-release tag) and reach a running JupyterLab.
    Only meaningful when setup-kind.sh preloaded the image (e2e-prerelease
    CI job); elsewhere the image isn't on the node, so skip."""
    import os

    import pytest

    if not os.environ.get("E2E_PRERELEASE"):
        pytest.skip("pre-release AF image not preloaded (E2E_PRERELEASE unset)")
    expected_image = os.environ["PRERELEASE_IMAGE"]

    client, _ = login("alice@purdue.edu")
    admin.delete("/hub/api/users/alice/server")  # tolerate leftovers (404 ok)
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if not admin.get("/hub/api/users/alice").json().get("servers"):
            break
        time.sleep(3)

    spawn = admin.post("/hub/api/users/alice/server", json={"profile": "pre-release"})
    assert spawn.status_code in (201, 202), spawn.text
    # generous: the real image's Lab cold-start on a 2-core runner is slow
    wait_ready(admin, "alice", timeout=600)

    pod = next(
        p
        for p in singleuser_pods()
        if p["metadata"]["labels"].get("username_unescaped") == "alice"
    )
    container = pod["spec"]["containers"][0]
    assert container["image"] == expected_image
    env = {e["name"]: e.get("value") for e in container["env"]}
    assert env.get("E2E_PROFILE_MARKER") == "pre-release"

    # The user's own session must reach the running Lab inside the AF image.
    lab = client.get(f"{HUB}/user/alice/api")
    assert lab.status_code == 200
    assert "version" in lab.json()


def test_production_profile_spawns_pinned_image(login, admin):
    """The "e2e = production" gate: the production JH option must spawn the
    exact image pinned in values.yaml (singleuser.image.tag) and reach a
    running JupyterLab. Proves the current hub config works with the image
    production actually runs — independent of what :pre-release moved to.
    Only meaningful when setup-kind.sh preloaded the image (e2e-production
    CI job); elsewhere the image isn't on the node, so skip."""
    import os

    import pytest

    if not os.environ.get("E2E_PRODUCTION"):
        pytest.skip("production AF image not preloaded (E2E_PRODUCTION unset)")
    expected_image = os.environ["PRODUCTION_IMAGE"]

    client, _ = login("bob@purdue.edu")
    admin.delete("/hub/api/users/bob/server")  # tolerate leftovers (404 ok)
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if not admin.get("/hub/api/users/bob").json().get("servers"):
            break
        time.sleep(3)

    spawn = admin.post("/hub/api/users/bob/server", json={"profile": "production"})
    assert spawn.status_code in (201, 202), spawn.text
    # generous: the real image's Lab cold-start on a 2-core runner is slow
    wait_ready(admin, "bob", timeout=600)

    pod = next(
        p
        for p in singleuser_pods()
        if p["metadata"]["labels"].get("username_unescaped") == "bob"
    )
    container = pod["spec"]["containers"][0]
    assert container["image"] == expected_image
    env = {e["name"]: e.get("value") for e in container["env"]}
    assert env.get("E2E_PROFILE_MARKER") == "production"

    # The user's own session must reach the running Lab inside the AF image.
    lab = client.get(f"{HUB}/user/bob/api")
    assert lab.status_code == 200
    assert "version" in lab.json()
