"""Dynamic profile discovery — reads JupyterHub profileList from the cluster ConfigMap.

The ConfigMap 'jupyterhub-config' in the cms namespace contains values.yaml as its
data key.  We parse singleuser.profileList from that YAML so the service stays in
sync with whatever the admin has configured — no hardcoded option keys or slugs.
"""

import re
import time
from typing import Optional

import httpx
import yaml

# Kubernetes in-cluster service and credentials
_K8S_API = "https://kubernetes.default.svc"
_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
_CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
_NAMESPACE = "cms"
_CONFIGMAP = "jupyterhub-config"

# (expiry_monotonic, profiles) — refresh every 5 minutes
_cache: tuple[float, list[dict]] | None = None
_CACHE_TTL = 300.0


# ── slug computation (mirrors KubeSpawner internals) ─────────────────────────

def _slug(display_name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", display_name.lower()).strip("-")


# ── ConfigMap fetch + parse ───────────────────────────────────────────────────

async def _read_configmap() -> Optional[str]:
    """Return the values.yaml string from the jupyterhub-config ConfigMap, or None."""
    try:
        token = open(_TOKEN_PATH).read().strip()
    except OSError:
        return None  # not running inside k8s (local dev)

    async with httpx.AsyncClient(verify=_CA_PATH) as client:
        try:
            resp = await client.get(
                f"{_K8S_API}/api/v1/namespaces/{_NAMESPACE}/configmaps/{_CONFIGMAP}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=5.0,
            )
        except httpx.RequestError:
            return None

    if resp.status_code != 200:
        return None

    return resp.json().get("data", {}).get("values.yaml")


def _parse_profiles(values_yaml: str) -> list[dict]:
    """Parse singleuser.profileList from the JupyterHub Helm values YAML."""
    try:
        values = yaml.safe_load(values_yaml)
    except yaml.YAMLError:
        return []

    raw = values.get("singleuser", {}).get("profileList", [])
    profiles: list[dict] = []

    for p in raw:
        display_name = p.get("display_name", "")
        is_default = bool(p.get("default", False))

        # Strip HTML from description
        raw_desc = p.get("description", "")
        description = re.sub(r"<[^>]+>", "", raw_desc).strip()

        options: dict[str, dict] = {}
        for opt_key, opt_val in (p.get("profile_options") or {}).items():
            choices: dict[str, str] = {}
            for ck, cv in (opt_val.get("choices") or {}).items():
                label = (
                    cv.get("display_name", str(ck))
                    if isinstance(cv, dict)
                    else str(cv)
                )
                if isinstance(cv, dict) and cv.get("default"):
                    label += " (default)"
                choices[str(ck)] = label

            options[str(opt_key)] = {
                "display_name": opt_val.get("display_name", str(opt_key)),
                "choices": choices,
            }

        profiles.append(
            {
                "display_name": display_name,
                "slug": _slug(display_name),
                "default": is_default,
                "description": description,
                "options": options,
            }
        )

    return profiles


# ── public helpers ────────────────────────────────────────────────────────────

async def get_profiles(force: bool = False) -> list[dict]:
    """Return the profile list, with a 5-minute cache."""
    global _cache
    now = time.monotonic()
    if not force and _cache and now < _cache[0]:
        return _cache[1]

    raw = await _read_configmap()
    if raw:
        profiles = _parse_profiles(raw)
        if profiles:
            _cache = (now + _CACHE_TTL, profiles)
            return profiles

    # Return stale cache rather than nothing
    return _cache[1] if _cache else []


def find_profile(profiles: list[dict], name: str) -> Optional[dict]:
    """Look up a profile by slug or display name, case-insensitive."""
    key = name.strip().lower()
    for p in profiles:
        if p["slug"] == key or p["display_name"].lower() == key:
            return p
    return None


# ── tool registration ─────────────────────────────────────────────────────────

def register(mcp) -> None:
    @mcp.tool()
    async def list_af_profiles() -> str:
        """List available Analysis Facility session profiles and their configurable options.

        Read this before calling start_af_session to know which profile slugs and
        option key/value pairs are valid.  The data is read live from the cluster
        configuration so it always reflects the current setup.
        """
        profiles = await get_profiles()
        if not profiles:
            return (
                "Could not read profile list from the cluster — "
                "the service may lack ConfigMap read access, or is running outside the cluster."
            )

        sections: list[str] = [f"# {len(profiles)} available profile(s)\n"]

        for p in profiles:
            header = f"## {p['display_name']}"
            if p["default"]:
                header += "  *(default)*"
            block = [
                header,
                f"slug: `\"{p['slug']}\"`",
            ]
            if p["description"]:
                block.append(f"_{p['description']}_")

            if p["options"]:
                block.append("\nOption keys and valid values:")
                for opt_key, opt_info in p["options"].items():
                    block.append(f"  **\"{opt_key}\"** — {opt_info['display_name']}")
                    for ck, label in opt_info["choices"].items():
                        block.append(f"    `\"{ck}\"` → {label}")

            sections.append("\n".join(block))

        return "\n\n".join(sections)
