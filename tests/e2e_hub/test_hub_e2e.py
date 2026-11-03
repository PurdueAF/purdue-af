"""End-to-end: real hub, real chart, real auth scripts — mock CILogon.

The production custom-spawner.py runs unmodified inside the hub; these tests
drive it through the actual OAuth code flow and the KubeSpawner spawn path.
"""

import base64
import json
import os
import subprocess
import time

import httpx
import pytest
from conftest import CERN_IDP, HUB, MOCK, PURDUE_IDP

# Kind hub stack lives in the production namespace (`cms`); kubectl must too.
E2E_NAMESPACE = "cms"


def kubectl_json(*args):
    return json.loads(
        subprocess.run(
            ["kubectl", "-n", E2E_NAMESPACE, *args, "-o", "json"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )


def live_pods():
    """Session pods, less any still terminating after a stop."""
    pods = kubectl_json("get", "pods", "-l", "component=singleuser-server")["items"]
    return [p for p in pods if not p["metadata"].get("deletionTimestamp")]


def pod_owner(pod):
    return pod["metadata"]["labels"].get("username_unescaped")


def user_pod(user):
    pods = [p for p in live_pods() if pod_owner(p) == user]
    assert pods, f"no pod of {user}; running: {[pod_owner(p) for p in live_pods()]}"
    return pods[0]


def pod_env(pod):
    return {e["name"]: e.get("value") for e in pod["spec"]["containers"][0]["env"]}


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


def respawn(admin, user, **request):
    """Stop whatever `user` runs (a 404 is fine), then spawn until ready.

    Generous timeout: the real image's Lab cold-start on a 2-core runner is slow.
    """
    admin.delete(f"/hub/api/users/{user}/server")
    wait_cleared(admin, user)
    spawn = admin.post(f"/hub/api/users/{user}/server", **request)
    assert spawn.status_code in (201, 202), spawn.text
    wait_ready(admin, user, timeout=600)


def require_image(marker):
    """Skip unless setup-kind.sh preloaded the real AF image `marker` stands for."""
    if not os.environ.get(marker):
        pytest.skip(f"AF image not preloaded ({marker} unset)")


def require_production():
    """Skip where the production image is not on the node: the plumbing tests
    run the real image, with no mock stand-in (E2E_PRODUCTION, set by the
    e2e-production CI job)."""
    require_image("E2E_PRODUCTION")


def assert_lab_opens_through_oauth(client, user):
    """Open `user`'s Lab as their browser would: jupyterhub-singleuser in the AF
    image sends a browser with no cookie of its own to the Hub for a code, then
    trades it for a token. /api answers without auth, so only this walk tests
    the Hub and singleuser versions against each other."""
    lab = client.get(f"/user/{user}/lab", timeout=120)
    hops = [r.url.path for r in lab.history]
    assert lab.status_code == 200, f"{lab.url.path}: {lab.status_code} after {hops}"
    assert lab.url.path == f"/user/{user}/lab", hops
    assert "/hub/api/oauth2/authorize" in hops, hops
    assert f"/user/{user}/oauth_callback" in hops, hops
    assert "jupyter-config-data" in lab.text


def assert_profile_opens_image(client, admin, user, profile, image):
    respawn(admin, user, json={"profile": profile})

    pod = user_pod(user)
    assert pod["spec"]["containers"][0]["image"] == image
    assert pod_env(pod).get("E2E_PROFILE_MARKER") == profile
    assert_lab_opens_through_oauth(client, user)


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


def test_mock_cilogon_rejects_unregistered_scopes():
    """Every login above passes only while the hub requests registered scopes."""
    response = httpx.get(
        f"{MOCK}/authorize",
        params={
            "redirect_uri": f"{HUB}/hub/oauth_callback",
            "scope": "openid profile",
            "state": "s",
        },
        follow_redirects=False,
        timeout=10,
    )
    assert response.status_code == 302
    assert "error=invalid_scope" in response.headers["location"]


def test_purdue_user_not_in_list_is_rejected(login):
    _, response = login("mallory@purdue.edu")
    assert response.status_code == 403


def test_cern_user_gets_suffixed_username(login, admin):
    client, response = login("carol@cern.ch", idp=CERN_IDP)
    assert response.status_code == 200

    user = admin.get("/hub/api/users/carol-cern")
    assert user.status_code == 200, "expected hub user 'carol-cern' to exist"


def test_unknown_domain_is_rejected(login):
    _, response = login("eve@evil.example")
    assert response.status_code == 403


def test_production_spawn_carries_ownership_label(login, admin):
    """Base of the plumbing chain: spawn alice on the DEFAULT profile (the
    production image, no mock stand-in) and assert the username_unescaped
    label — the agentic-interface ownership gate. Real image, so gated on
    E2E_PRODUCTION (preloaded in the e2e-production job; skipped elsewhere).
    That a real AF session reaches a running Lab is asserted by
    test_production_profile_spawns_pinned_image below."""
    require_production()
    login("alice@purdue.edu")
    respawn(admin, "alice", json={})

    assert "alice" in [pod_owner(p) for p in live_pods()]


# ── session lifecycle & isolation (tests below build on alice's running server) ──


def test_ldap_uid_gid_land_in_pod(admin):
    """set-user-info.py auth_state_hook: the LDAP-resolved uid/gid of a
    Purdue user must reach the pod env (values from mock-ldap-users.ldif).
    Runs against alice's server spawned above."""
    require_production()
    env = pod_env(user_pod("alice"))
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

    assert {"alice", "carol-cern"} <= {pod_owner(p) for p in live_pods()}


def test_profile_selection_lands_in_pod(admin):
    """Explicit profile choice (the start_af_session contract) must reach the pod."""
    require_production()
    respawn(admin, "alice", json={"profile": "e2e-alt-profile"})

    assert pod_env(user_pod("alice")).get("E2E_PROFILE_MARKER") == "alt"


def test_bodiless_spawn_with_stale_saved_profile_starts(admin):
    """A spawn request without a body reuses the saved user_options; a saved
    profile slug that no longer exists must spawn the default profile."""
    require_production()
    stale = {
        "profile": "purdue-af-0-12-3-pixi-based-environment-management",
        "cpu": "2",
    }
    for body in ({"json": stale}, {}):
        respawn(admin, "alice", **body)

        # the bodiless spawn carries the saved options, remapped by the hook
        server = admin.get("/hub/api/users/alice").json()["servers"][""]
        assert server["user_options"] == {**stale, "profile": "production"}
        assert pod_env(user_pod("alice")).get("E2E_PROFILE_MARKER") == "production"


def test_stop_server_cleans_up(admin):
    """Stopping must remove the pod and clear hub state (carol's untouched)."""
    require_production()
    stop = admin.delete("/hub/api/users/alice/server")
    assert stop.status_code in (202, 204), stop.text

    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        owners = {pod_owner(p) for p in live_pods()}
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
    users = base64.b64encode(b"alice\nbob\nadmin-e2e\ndave\n").decode()
    subprocess.run(
        [
            "kubectl",
            "-n",
            E2E_NAMESPACE,
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
    """The authenticator's admin_users must translate into the hub admin flag."""
    login("admin-e2e@purdue.edu")
    user = admin.get("/hub/api/users/admin-e2e").json()
    assert user.get("admin") is True


def test_tampered_oauth_state_is_rejected(login):
    """CSRF gate: a forged callback (bad state) must not create a session."""
    client = httpx.Client(base_url=HUB, follow_redirects=True, timeout=30)
    response = client.get("/hub/oauth_callback?code=forged&state=garbage")
    client.close()
    assert response.status_code == 400


def test_logout_clears_session(login):
    """auto_login means logout is followed by silent re-auth on the next
    request — so prove the OLD session died by making the re-auth identity
    a denied user. A surviving cookie would return 200 without consulting
    the mock; a real logout forces the OAuth round trip into the gate."""
    client, _ = login("alice@purdue.edu")
    assert client.get("/hub/home").status_code == 200

    client.get("/hub/logout", follow_redirects=False)
    httpx.post(
        f"{MOCK}/_identity",
        json={"eppn": "mallory@purdue.edu", "idp": PURDUE_IDP},
        timeout=10,
    )
    response = client.get("/hub/home")  # follows into OAuth as mallory
    assert response.status_code == 403  # denied: old session is gone


def test_prerelease_profile_spawns_ci_built_image(login, admin):
    """The CD gate for the AF image: the pre-release JH option must spawn the
    exact image in PRERELEASE_IMAGE and open a JupyterLab. Runs in the
    e2e-pre-release CI job, where setup-kind.sh preloaded that image."""
    require_image("E2E_PRERELEASE")
    client, _ = login("alice@purdue.edu")
    assert_profile_opens_image(
        client, admin, "alice", "pre-release", os.environ["PRERELEASE_IMAGE"]
    )


def test_production_profile_spawns_pinned_image(login, admin):
    """The "e2e = production" gate: the production JH option must spawn the
    exact image pinned in values.yaml (singleuser.image.tag) and open a
    JupyterLab. Proves the current hub config works with the image production
    actually runs, independent of what :pre-release moved to."""
    require_production()
    client, _ = login("bob@purdue.edu")
    assert_profile_opens_image(
        client, admin, "bob", "production", os.environ["PRODUCTION_IMAGE"]
    )
