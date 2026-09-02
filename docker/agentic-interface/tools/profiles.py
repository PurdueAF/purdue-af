"""Dynamic profile discovery — reads JupyterHub profileList from the cluster ConfigMap.

The ConfigMap 'jupyterhub-config' in the cms namespace contains values.yaml as its
data key.  We parse singleuser.profileList from that YAML so the service stays in
sync with whatever the admin has configured — no hardcoded option keys or slugs.
"""

import asyncio
import os
import re
from typing import Any, Optional

import httpx
import yaml
from cachetools import TTLCache
from errors import UpstreamError, describe_exception, json_body, response_detail
from shared import shared_client

from tools.gpu import free_gpus, gpu_error

# Kubernetes in-cluster service and credentials
_K8S_API = "https://kubernetes.default.svc"
_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
_CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
_NAMESPACE = os.environ.get("NAMESPACE", "cms")
_CONFIGMAP = "jupyterhub-config"

# Fresh for 5 minutes; the last good read is kept separately so a broken
# source degrades to stale data rather than nothing.
_CACHE_TTL = 300.0
_fresh: TTLCache[str, list[dict]] = TTLCache(1, _CACHE_TTL)
_last_good: list[dict] = []
# Why the last read failed, in user terms — reported instead of a bare
# "service may be misconfigured". None after a successful read.
_last_error: Optional[str] = None
# Single-flights the refresh so concurrent cache misses don't dogpile the API.
_refresh_lock = asyncio.Lock()


# ── slug computation (mirrors KubeSpawner internals) ─────────────────────────


def _slug(display_name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", display_name.lower()).strip("-")


def _gpu_label(label: str, resource: Optional[str], free: Optional[dict]) -> str:
    """Append live availability to a GPU choice label (marks exhausted flavors)."""
    if not resource or free is None:
        return label
    base = label.removesuffix(" - subject to availability")
    count = free.get(resource, 0)
    if count <= 0:
        return f"{base} — none available right now (do not select)"
    return f"{base} — {count} available now"


def _gpu_resource(kubespawner_override: Optional[dict]) -> Optional[str]:
    """Return the k8s GPU resource a choice requests (amount > 0), else None."""
    limits = (kubespawner_override or {}).get("extra_resource_limits") or {}
    for resource, amount in limits.items():
        if str(resource).startswith("nvidia.com/"):
            try:
                if int(amount) > 0:
                    return str(resource)
            except (TypeError, ValueError):
                continue
    return None


# ── ConfigMap fetch + parse ───────────────────────────────────────────────────


async def _read_configmap() -> Optional[str]:
    """Return the values.yaml string from the jupyterhub-config ConfigMap, or None.

    Every None is accompanied by ``_last_error`` saying why.
    """
    global _last_error
    try:
        token = open(_TOKEN_PATH).read().strip()
    except OSError:
        # not running inside k8s (local dev)
        _last_error = (
            "this service is not running inside Kubernetes (no service-account "
            "token), so the JupyterHub configuration cannot be read"
        )
        return None

    # verify= goes to the transport: httpx ignores client-level TLS settings
    # once a custom transport is supplied (applied on first creation only).
    client = shared_client("kubernetes-api", verify=_CA_PATH)
    try:
        resp = await client.get(
            f"{_K8S_API}/api/v1/namespaces/{_NAMESPACE}/configmaps/{_CONFIGMAP}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=5.0,
        )
    except httpx.RequestError as exc:
        _last_error = f"Kubernetes API unreachable — {describe_exception(exc)}"
        return None

    if resp.status_code == 403:
        _last_error = (
            "the agentic-interface service account is not allowed to read "
            f"ConfigMap '{_CONFIGMAP}' in namespace '{_NAMESPACE}' (Kubernetes "
            "API HTTP 403) — an RBAC problem on the facility side"
        )
        return None
    if resp.status_code == 404:
        _last_error = (
            f"ConfigMap '{_CONFIGMAP}' does not exist in namespace '{_NAMESPACE}' "
            "(Kubernetes API HTTP 404)"
        )
        return None
    if resp.status_code != 200:
        detail = response_detail(resp, limit=120)
        _last_error = f"Kubernetes API returned HTTP {resp.status_code}" + (
            f" — {detail}" if detail else ""
        )
        return None

    payload = json_body(resp)
    data = payload.get("data") if isinstance(payload, dict) else None
    values: Any = data.get("values.yaml") if isinstance(data, dict) else None
    if values is None:
        _last_error = f"ConfigMap '{_CONFIGMAP}' has no values.yaml entry"
        return None
    _last_error = None
    return str(values)


def _parse_profiles(values_yaml: str) -> list[dict]:
    """Parse singleuser.profileList from the JupyterHub Helm values YAML."""
    try:
        values = yaml.safe_load(values_yaml)
    except yaml.YAMLError:
        return []
    if not isinstance(values, dict):
        return []

    singleuser = values.get("singleuser") or {}
    raw = singleuser.get("profileList") if isinstance(singleuser, dict) else None
    if not isinstance(raw, list):
        return []
    profiles: list[dict] = []

    for p in raw:
        if not isinstance(p, dict):
            continue
        display_name = p.get("display_name", "")
        is_default = bool(p.get("default", False))

        # Strip HTML from description
        raw_desc = p.get("description", "")
        description = re.sub(r"<[^>]+>", "", raw_desc).strip()

        options: dict[str, dict] = {}
        for opt_key, opt_val in (p.get("profile_options") or {}).items():
            choices: dict[str, str] = {}
            gpu: dict[str, str] = {}  # choice key -> k8s GPU resource (amount > 0)
            for ck, cv in (opt_val.get("choices") or {}).items():
                label = (
                    cv.get("display_name", str(ck)) if isinstance(cv, dict) else str(cv)
                )
                if isinstance(cv, dict) and cv.get("default"):
                    label += " (default)"
                choices[str(ck)] = label
                if isinstance(cv, dict):
                    resource = _gpu_resource(cv.get("kubespawner_override"))
                    if resource:
                        gpu[str(ck)] = resource

            option = {
                "display_name": opt_val.get("display_name", str(opt_key)),
                "choices": choices,
            }
            if gpu:
                option["gpu"] = gpu
            options[str(opt_key)] = option

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


def profiles_error() -> Optional[str]:
    """Why the profile list is (or was last) unavailable, or None."""
    return _last_error


async def get_profiles(force: bool = False) -> list[dict]:
    """Return the profile list, with a 5-minute cache.

    An empty list always has a reason waiting in ``profiles_error()``.
    """
    global _last_good, _last_error
    if not force and "profiles" in _fresh:
        return _fresh["profiles"]

    async with _refresh_lock:
        # Re-check after acquiring — another task may have refreshed while
        # we waited on the lock.
        if not force and "profiles" in _fresh:
            return _fresh["profiles"]

        raw = await _read_configmap()
        if raw:
            profiles = _parse_profiles(raw)
            if profiles:
                _fresh["profiles"] = profiles
                _last_good = profiles
                return profiles
            _last_error = (
                "the JupyterHub configuration was read but contains no parseable "
                "singleuser.profileList"
            )

        # Return stale data rather than nothing
        return _last_good


def find_profile(profiles: list[dict], name: str) -> Optional[dict]:
    """Look up a profile by slug or display name, case-insensitive."""
    key = name.strip().lower()
    for p in profiles:
        if p["slug"] == key or p["display_name"].lower() == key:
            return p
    return None


# ── tool registration ─────────────────────────────────────────────────────────


def register(mcp: Any) -> None:
    @mcp.tool()
    async def list_af_profiles() -> str:
        """List available Analysis Facility session profiles and their configurable options.

        Read this before calling start_af_session to know which profile slugs and
        option key/value pairs are valid.  The data is read live from the current
        AF configuration so it always reflects the current setup.
        """
        profiles = await get_profiles()
        if not profiles:
            raise UpstreamError(
                f"Could not read profile list — {profiles_error() or 'unknown reason'}. "
                "This is a problem on the facility side, not with the request: "
                "try again in a minute, and contact AF support if it persists. "
                "start_af_session(use_defaults=True) still works — the Hub "
                "applies its own default profile."
            )

        # Live GPU availability (only queried if some profile exposes a GPU option).
        has_gpu = any("gpu" in opt for p in profiles for opt in p["options"].values())
        free = await free_gpus() if has_gpu else None

        sections: list[str] = [f"# {len(profiles)} available profile(s)\n"]

        for p in profiles:
            header = f"## {p['display_name']}"
            if p["default"]:
                header += "  *(default)*"
            block = [
                header,
                f'slug: `"{p["slug"]}"`',
            ]
            if p["description"]:
                block.append(f"_{p['description']}_")

            if p["options"]:
                block.append("\nOption keys and valid values:")
                for opt_key, opt_info in p["options"].items():
                    block.append(f'  **"{opt_key}"** — {opt_info["display_name"]}')
                    gpu_map = opt_info.get("gpu") or {}
                    for ck, label in opt_info["choices"].items():
                        block.append(
                            f'    `"{ck}"` → {_gpu_label(label, gpu_map.get(ck), free)}'
                        )

            sections.append("\n".join(block))

        if has_gpu and free is None:
            sections.append(
                "Note: live GPU availability is unknown right now "
                f"({gpu_error() or 'the monitoring system did not answer'}), so GPU "
                "choices are listed without live counts and a GPU flavour may "
                "turn out to be exhausted when the session starts."
            )

        return "\n\n".join(sections)
