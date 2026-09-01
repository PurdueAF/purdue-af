"""Cross-tool helpers: pooled HTTP clients and query-label escaping.

Every tool used to build a fresh ``httpx.AsyncClient`` per call, paying a TCP
handshake each time; ``shared_client`` keeps one pooled client per backend
target instead (auth.py predates this and keeps its own). Clients live for the
process — never ``async with`` them closed at a call site.

``quote_label`` escapes a value for interpolation inside a PromQL/LogQL label
matcher (``{label="<value>"}``). Usernames come from the Hub and are the only
dynamic values we interpolate, but escaping centrally removes the injection
class outright.
"""

from typing import Any, Callable, Union

import httpx
from metrics import instrumented_transport

_clients: dict[str, httpx.AsyncClient] = {}


def shared_client(
    name: str,
    *,
    target: Union[str, Callable[[httpx.Request], str], None] = None,
    **transport_kwargs: Any,
) -> httpx.AsyncClient:
    """Return the process-wide pooled client for ``name``, creating it once.

    ``target`` is the upstream-metrics label (defaults to ``name``); it may be
    a callable when one client talks to several backends (see dask.py).
    ``transport_kwargs`` (e.g. ``verify=``) apply only on first creation.
    """
    client = _clients.get(name)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(
            transport=instrumented_transport(target or name, **transport_kwargs)
        )
        _clients[name] = client
    return client


def quote_label(value: str) -> str:
    """Escape a string for use inside a double-quoted PromQL/LogQL label value."""
    return value.replace("\\", "\\\\").replace('"', '\\"')
